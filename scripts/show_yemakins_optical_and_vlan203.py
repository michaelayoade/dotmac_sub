from __future__ import annotations

import sys
import time

from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services.network.olt_ssh import (
    _open_shell,
    _read_until_prompt,
    _run_huawei_cmd,
)
from app.services.network.olt_ssh_diagnostics import get_ont_optical_info
from app.services.network.olt_ssh_ont.iphost import get_ont_iphost_config
from app.services.network.olt_ssh_ont.omci_config import read_ont_wan_config

OLT_DB_ID = "bd2dbc50-90db-4f03-8670-8dc708053f06"
FSP = "0/1/7"
ONT_ID = 1


def main() -> int:
    started = time.time()
    with SessionLocal() as db:
        olt = db.get(OLTDevice, OLT_DB_ID)
        if olt is None:
            print("OLT not found", flush=True)
            return 2

        print(
            {
                "step": "context",
                "elapsed_s": round(time.time() - started, 2),
                "olt": olt.name,
                "host": olt.mgmt_ip or olt.hostname,
                "fsp": FSP,
                "ont_id": ONT_ID,
            },
            flush=True,
        )

        optical_ok, optical_msg, optical = get_ont_optical_info(olt, FSP, ONT_ID)
        print(
            {
                "step": "optical",
                "elapsed_s": round(time.time() - started, 2),
                "ok": optical_ok,
                "msg": optical_msg,
                "data": None
                if optical is None
                else {
                    "rx_power_dbm": optical.rx_power_dbm,
                    "tx_power_dbm": optical.tx_power_dbm,
                    "olt_rx_power_dbm": optical.olt_rx_power_dbm,
                    "temperature_c": optical.temperature_c,
                    "voltage_v": optical.voltage_v,
                    "bias_current_ma": optical.bias_current_ma,
                },
            },
            flush=True,
        )
        if optical is not None:
            print("----- RAW OPTICAL -----", flush=True)
            print(optical.raw, flush=True)

        ip_ok, ip_msg, ip_cfg = get_ont_iphost_config(olt, FSP, ONT_ID)
        print(
            {
                "step": "ipconfig",
                "elapsed_s": round(time.time() - started, 2),
                "ok": ip_ok,
                "msg": ip_msg,
                "data": ip_cfg,
            },
            flush=True,
        )

        wan_ok, wan_msg, wan_data = read_ont_wan_config(olt, FSP, ONT_ID)
        print(
            {
                "step": "wan_readback",
                "elapsed_s": round(time.time() - started, 2),
                "ok": wan_ok,
                "msg": wan_msg,
                "data": wan_data,
            },
            flush=True,
        )

        transport, channel, policy = _open_shell(olt)
        try:
            channel.send("enable\n")
            _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
            channel.send("screen-length 0 temporary\n")
            _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

            for command in (
                "display ont optical-info 0 1 7 1",
                "display ont ipconfig 0 1 7 1",
                "display ont internet-config 7 1",
                "display ont internet-config 0/1/7 1",
                "display ont wan-config 7 1",
                "display ont wan-config 0/1/7 1",
                "display service-port 100",
            ):
                output = _run_huawei_cmd(channel, command, prompt=policy.prompt_regex)
                print(f"----- {command} -----", flush=True)
                print(output, flush=True)
        finally:
            transport.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
