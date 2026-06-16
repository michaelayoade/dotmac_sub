from __future__ import annotations

import sys
import time

from app.db import SessionLocal
from app.models.network import OLTDevice, OntUnit
from app.services.network.huawei_command_profiles import get_huawei_command_profile
from app.services.network.olt_ssh import (
    _open_shell,
    _read_until_prompt,
    _run_huawei_cmd,
    _run_huawei_paged_cmd,
    is_error_output,
)
from app.services.network.olt_ssh_ont.status import find_ont_by_serial
from app.services.network.serial_utils import parse_ont_id_on_olt

ONT_DB_ID = "427f7641-3de1-4277-8726-1f30bd441249"
OLT_DB_ID = "bd2dbc50-90db-4f03-8670-8dc708053f06"


def main() -> int:
    started = time.time()
    with SessionLocal() as db:
        olt = db.get(OLTDevice, OLT_DB_ID)
        ont = db.get(OntUnit, ONT_DB_ID)
        if olt is None or ont is None:
            print("Missing OLT or ONT record", flush=True)
            return 2

        fsp = (
            f"{(ont.board or '').strip()}/{(ont.port or '').strip()}"
            if ont.board and ont.port
            else None
        )
        ont_id = parse_ont_id_on_olt(ont.external_id)
        if not fsp or ont_id is None:
            print(
                {
                    "error": "ONT placement incomplete",
                    "board": ont.board,
                    "port": ont.port,
                    "external_id": ont.external_id,
                },
                flush=True,
            )
            return 2

        print(
            {
                "step": "resolved_context",
                "elapsed_s": round(time.time() - started, 2),
                "olt": olt.name,
                "host": olt.mgmt_ip or olt.hostname,
                "ssh_user": olt.ssh_username,
                "serial": ont.serial_number,
                "fsp": fsp,
                "ont_id": ont_id,
            },
            flush=True,
        )

        lookup_ok, lookup_msg, lookup_entry = find_ont_by_serial(olt, ont.serial_number)
        print(
            {
                "step": "serial_lookup",
                "elapsed_s": round(time.time() - started, 2),
                "lookup_ok": lookup_ok,
                "lookup_msg": lookup_msg,
                "lookup_entry": None
                if lookup_entry is None
                else {
                    "fsp": lookup_entry.fsp,
                    "ont_id": lookup_entry.onu_id,
                    "run_state": lookup_entry.run_state,
                    "serial": lookup_entry.real_serial,
                },
            },
            flush=True,
        )

        vendor_serial = (ont.vendor_serial_number or "").strip().upper()
        if vendor_serial and vendor_serial != (ont.serial_number or "").strip().upper():
            lookup_ok2, lookup_msg2, lookup_entry2 = find_ont_by_serial(olt, vendor_serial)
            print(
                {
                    "step": "vendor_serial_lookup",
                    "elapsed_s": round(time.time() - started, 2),
                    "lookup_ok": lookup_ok2,
                    "lookup_msg": lookup_msg2,
                    "lookup_entry": None
                    if lookup_entry2 is None
                    else {
                        "fsp": lookup_entry2.fsp,
                        "ont_id": lookup_entry2.onu_id,
                        "run_state": lookup_entry2.run_state,
                        "serial": lookup_entry2.real_serial,
                    },
                },
                flush=True,
            )

        transport, channel, policy = _open_shell(olt)
        try:
            print(
                {
                    "step": "ssh_opened",
                    "elapsed_s": round(time.time() - started, 2),
                    "prompt_regex": policy.prompt_regex,
                },
                flush=True,
            )
            channel.send("enable\n")
            _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)
            channel.send("screen-length 0 temporary\n")
            _read_until_prompt(channel, policy.prompt_regex, timeout_sec=5)

            command = get_huawei_command_profile(olt).display_ont_info(fsp, ont_id)
            print(
                {
                    "step": "running_command",
                    "elapsed_s": round(time.time() - started, 2),
                    "command": command,
                },
                flush=True,
            )
            output = _run_huawei_cmd(channel, command, prompt=policy.prompt_regex)
            if is_error_output(output):
                print(
                    {
                        "step": "olt_error",
                        "elapsed_s": round(time.time() - started, 2),
                        "tail": output.splitlines()[-20:],
                    },
                    flush=True,
                )
                return 1

            run_state = None
            config_state = None
            match_state = None
            for line in output.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key_norm = key.strip().lower()
                value_text = value.strip()
                if key_norm == "run state":
                    run_state = value_text
                elif key_norm == "config state":
                    config_state = value_text
                elif key_norm == "match state":
                    match_state = value_text

            print(
                {
                    "step": "command_complete",
                    "elapsed_s": round(time.time() - started, 2),
                    "run_state": run_state,
                    "config_state": config_state,
                    "match_state": match_state,
                },
                flush=True,
            )
            print("----- RAW OUTPUT START -----", flush=True)
            print(output, flush=True)
            print("----- RAW OUTPUT END -----", flush=True)

            fragments = [
                (ont.serial_number or "").strip().upper(),
                vendor_serial,
                "7D4518C3",
                "485754437D4518C3",
            ]
            summary_output = _run_huawei_paged_cmd(
                channel,
                "display ont info summary all",
                prompt=policy.prompt_regex,
                timeout_sec=90,
            )
            summary_hits = [
                line
                for line in summary_output.splitlines()
                if any(fragment and fragment in line.upper() for fragment in fragments)
            ]
            print(
                {
                    "step": "summary_search",
                    "elapsed_s": round(time.time() - started, 2),
                    "fragments": [fragment for fragment in fragments if fragment],
                    "hit_count": len(summary_hits),
                    "hits": summary_hits[:20],
                },
                flush=True,
            )

            autofind_output = _run_huawei_paged_cmd(
                channel,
                "display ont autofind all",
                prompt=policy.prompt_regex,
                timeout_sec=90,
            )
            autofind_hits = [
                line
                for line in autofind_output.splitlines()
                if any(fragment and fragment in line.upper() for fragment in fragments)
            ]
            print(
                {
                    "step": "autofind_search",
                    "elapsed_s": round(time.time() - started, 2),
                    "hit_count": len(autofind_hits),
                    "hits": autofind_hits[:20],
                },
                flush=True,
            )
            return 0
        finally:
            transport.close()


if __name__ == "__main__":
    sys.exit(main())
