from __future__ import annotations

import sys
import time

from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services.network.olt_ssh import (
    _open_shell,
    _read_until_prompt,
    _run_huawei_cmd,
    _run_huawei_paged_cmd,
    get_service_ports_for_ont,
)
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

        ports_ok, ports_msg, ports = get_service_ports_for_ont(olt, FSP, ONT_ID)
        print(
            {
                "step": "service_ports",
                "elapsed_s": round(time.time() - started, 2),
                "ok": ports_ok,
                "msg": ports_msg,
                "ports": [
                    {
                        "index": port.index,
                        "vlan_id": port.vlan_id,
                        "ont_id": port.ont_id,
                        "gem_index": port.gem_index,
                        "flow_type": port.flow_type,
                        "flow_para": port.flow_para,
                        "state": port.state,
                        "fsp": port.fsp,
                        "tag_transform": port.tag_transform,
                    }
                    for port in ports
                    if port.vlan_id in {201, 203}
                ],
            },
            flush=True,
        )

        transport, channel, policy = _open_shell(olt)
        try:
            channel.send("enable\n")
            _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
            channel.send("screen-length 0 temporary\n")
            _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

            wan_raw_commands = [
                "display ont wan-config 7 1",
                "display ont wan-config 0/1/7 1",
            ]
            for command in wan_raw_commands:
                output = _run_huawei_cmd(channel, command, prompt=policy.prompt_regex)
                print(f"----- {command} -----", flush=True)
                print(output, flush=True)

            sp_command = "display service-port port 0/1/7"
            sp_output = _run_huawei_paged_cmd(
                channel,
                sp_command,
                prompt=policy.prompt_regex,
                timeout_sec=90,
            )
            print(f"----- {sp_command} -----", flush=True)
            print(sp_output, flush=True)
        finally:
            transport.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
