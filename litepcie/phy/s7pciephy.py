import os

from migen import *
from migen.genlib.cdc import MultiReg
from migen.genlib.misc import WaitTimer

from litex.soc.interconnect.csr import *

from litepcie.common import *


class S7PCIEPHY(Module, AutoCSR):
    def __init__(self, platform, pads, data_width=64, bar0_size=1*MB, cd="sys", pll1=None):
        self.sink = stream.Endpoint(phy_layout(data_width))
        self.source = stream.Endpoint(phy_layout(data_width))
        self.msi = stream.Endpoint(msi_layout())

        self._lnk_up = CSRStatus()
        self._msi_enable = CSRStatus()
        self._bus_master_enable = CSRStatus()
        self._max_request_size = CSRStatus(16)
        self._max_payload_size = CSRStatus(16)

        self.data_width = data_width

        self.id = Signal(16)
        self.bar0_size = bar0_size
        self.bar0_mask = get_bar_mask(bar0_size)
        self.max_request_size = Signal(16)
        self.max_payload_size = Signal(16)

        # # #

        # clocking
        pcie_clk = Signal()
        pcie_rst = Signal()
        pcie_refclk = Signal()
        self.specials += Instance("IBUFDS_GTE2",
            i_CEB=0,
            i_I=pads.clk_p,
            i_IB=pads.clk_n,
            o_O=pcie_refclk
        )
        pcie_refclk.attr.add("keep")
        platform.add_period_constraint(pcie_refclk, 10.0)

        self.clock_domains.cd_pcie = ClockDomain()
        self.clock_domains.cd_pcie_reset_less = ClockDomain(reset_less=True)
        self.cd_pcie.clk.attr.add("keep")
        platform.add_period_constraint(self.cd_pcie.clk, 8.0)

        pcie_refclk_present = Signal()
        pcie_refclk_timer = ClockDomainsRenamer("pcie_reset_less")(WaitTimer(1024))
        self.submodules += pcie_refclk_timer
        self.comb += [
            pcie_refclk_timer.wait.eq(1),
            pcie_refclk_present.eq(pcie_refclk_timer.done)
        ]

        self.comb += [
            self.cd_pcie.clk.eq(pcie_clk),
            self.cd_pcie.rst.eq(pcie_rst & pcie_refclk_present),
            self.cd_pcie_reset_less.clk.eq(pcie_clk),
        ]

        # tx cdc (fpga --> host)
        if cd == "pcie":
            s_axis_tx = self.sink
        else:
            tx_buffer = stream.Buffer(phy_layout(data_width))
            tx_buffer = ClockDomainsRenamer(cd)(tx_buffer)
            tx_cdc = stream.AsyncFIFO(phy_layout(data_width), 4)
            tx_cdc = ClockDomainsRenamer({"write": cd, "read": "pcie"})(tx_cdc)
            self.submodules += tx_buffer, tx_cdc
            self.comb += [
                self.sink.connect(tx_buffer.sink),
                tx_buffer.source.connect(tx_cdc.sink)
            ]
            s_axis_tx = tx_cdc.source

        # rx cdc (host --> fpga)
        if cd == "pcie":
            m_axis_rx = self.source
        else:
            rx_cdc = stream.AsyncFIFO(phy_layout(data_width), 4)
            rx_cdc = ClockDomainsRenamer({"write": "pcie", "read": cd})(rx_cdc)
            rx_buffer = stream.Buffer(phy_layout(data_width))
            rx_buffer = ClockDomainsRenamer(cd)(rx_buffer)
            self.submodules += rx_buffer, rx_cdc
            self.comb += [
                rx_cdc.source.connect(rx_buffer.sink),
                rx_buffer.source.connect(self.source)
            ]
            m_axis_rx = rx_cdc.sink

        # msi cdc (fpga --> host)
        if cd == "pcie":
            cfg_msi = self.msi
        else:
            msi_cdc = stream.AsyncFIFO(msi_layout(), 4)
            msi_cdc = ClockDomainsRenamer({"write": cd, "read": "pcie"})(msi_cdc)
            self.submodules += msi_cdc
            self.comb += self.msi.connect(msi_cdc.sink)
            cfg_msi = msi_cdc.source


        # config
        def convert_size(command, size):
            cases = {}
            value = 128
            for i in range(6):
                cases[i] = size.eq(value)
                value = value*2
            return Case(command, cases)

        lnk_up = Signal()
        msienable = Signal()
        bus_number = Signal(8)
        device_number = Signal(5)
        function_number = Signal(3)
        command = Signal(16)
        dcommand = Signal(16)
        self.sync.pcie += [
            convert_size(dcommand[12:15], self.max_request_size),
            convert_size(dcommand[5:8], self.max_payload_size),
            self.id.eq(Cat(function_number, device_number, bus_number))
        ]
        self.specials += [
            MultiReg(lnk_up, self._lnk_up.status),
            MultiReg(command[2], self._bus_master_enable.status),
            MultiReg(msienable, self._msi_enable.status),
            MultiReg(self.max_request_size, self._max_request_size.status),
            MultiReg(self.max_payload_size, self._max_payload_size.status)
        ]

        # hard ip
        self.specials += Instance("pcie_phy",
                p_C_DATA_WIDTH=data_width,
                p_C_PCIE_GT_DEVICE={
                    "xc7k": "GTX",
                    "xc7a": "GTP"}[platform.device[:4]],
                p_C_BAR0=get_bar_mask(bar0_size),

                i_sys_clk=pcie_refclk,
                i_sys_rst_n=1 if not hasattr(pads, "rst_n") else pads.rst_n,

                o_pci_exp_txp=pads.tx_p,
                o_pci_exp_txn=pads.tx_n,

                i_pci_exp_rxp=pads.rx_p,
                i_pci_exp_rxn=pads.rx_n,

                o_user_clk=pcie_clk,
                o_user_reset=pcie_rst,
                o_user_lnk_up=lnk_up,

                #o_tx_buf_av=,
                #o_tx_terr_drop=,
                #o_tx_cfg_req=,
                i_tx_cfg_gnt=1,

                i_s_axis_tx_tvalid=s_axis_tx.valid,
                i_s_axis_tx_tlast=s_axis_tx.last,
                o_s_axis_tx_tready=s_axis_tx.ready,
                i_s_axis_tx_tdata=s_axis_tx.dat,
                i_s_axis_tx_tkeep=s_axis_tx.be,
                i_s_axis_tx_tuser=0,

                i_rx_np_ok=1,
                i_rx_np_req=1,

                o_m_axis_rx_tvalid=m_axis_rx.valid,
                o_m_axis_rx_tlast=m_axis_rx.last,
                i_m_axis_rx_tready=m_axis_rx.ready,
                o_m_axis_rx_tdata=m_axis_rx.dat,
                o_m_axis_rx_tkeep=m_axis_rx.be,
                #o_m_axis_rx_tuser=,

                #o_cfg_to_turnoff=,
                o_cfg_bus_number=bus_number,
                o_cfg_device_number=device_number,
                o_cfg_function_number=function_number,
                o_cfg_command=command,
                o_cfg_dcommand=dcommand,
                o_cfg_interrupt_msienable=msienable,

                i_cfg_interrupt=cfg_msi.valid,
                o_cfg_interrupt_rdy=cfg_msi.ready,
                i_cfg_interrupt_di=cfg_msi.dat,

                p_QPLL_PLL1_FBDIV=4 if pll1 is None else pll1.config["n2"],
                p_QPLL_PLL1_FBDIV_45=4 if pll1 is None else pll1.config["n1"],
                p_QPLL_PLL1_REFCLK_DIV=1 if pll1 is None else pll1.config["m"],

                i_QPLL_GTGREFCLK1=0 if pll1 is None else pll1.gtgrefclk,
                i_QPLL_GTREFCLK1=0 if pll1 is None else pll1.gtrefclk,
                i_QPLL_PLL1LOCKEN=1,
                i_QPLL_PLL1PD=1 if pll1 is None else 0,
                i_QPLL_PLL1REFCLKSEL=0b001 if pll1 is None else pll1.refclksel,
                i_QPLL_PLL1RESET=1 if pll1 is None else pll1.reset,
                o_QPLL_PLL1LOCK=Signal() if pll1 is None else pll1.lock,
                o_QPLL_PLL1OUTCLK=Signal() if pll1 is None else pll1.clk,
                o_QPLL_PLL1OUTREFCLK=Signal() if pll1 is None else pll1.refclk
        )
        litepcie_phy_path = os.path.abspath(os.path.dirname(__file__))
        platform.add_source_dir(os.path.join(litepcie_phy_path, "xilinx", "7-series", "common"))
        platform.add_source(os.path.join(litepcie_phy_path, "xilinx", "7-series", "common", "xpm_cdc.sv"))
        if platform.device[:4] == "xc7k":
            platform.add_source_dir(os.path.join(litepcie_phy_path, "xilinx", "7-series", "kintex7"))
        elif platform.device[:4] == "xc7a":
            platform.add_source_dir(os.path.join(litepcie_phy_path, "xilinx", "7-series", "artix7"))
