from __future__ import annotations

print("start", flush=True)

from app.db import SessionLocal
from app.models.network import OLTDevice
from app.services.network import olt_ssh as core

OLT_ID = "c1e3f7d2-b3fb-4adf-bec7-2ed1dba12cc7"


def main() -> int:
    db = SessionLocal()
    try:
        olt = db.get(OLTDevice, OLT_ID)
        print("olt_loaded", bool(olt), flush=True)
        if olt is None:
            return 1
        transport, channel, _policy = core._open_shell(olt)
        try:
            channel.send("enable\n")
            core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
            config_prompt = r"[#)]\s*$"
            core._run_huawei_cmd(channel, "config", prompt=config_prompt)
            channel.send("interface gpon 0/1\n")
            core._read_until_prompt(channel, config_prompt, timeout_sec=8)

            print("internet_config_cmd", flush=True)
            channel.send("ont internet-config 14 2 ip-index 1\n")
            out = core._read_until_prompt(channel, r"[#)]\s*$|<cr>", timeout_sec=12)
            if "<cr>" in out:
                channel.send("\n")
                out += core._read_until_prompt(channel, config_prompt, timeout_sec=10)
            print(out, flush=True)

            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
            print("wan_profile_all", flush=True)
            channel.send("display ont wan-profile all\n")
            out2 = core._read_until_prompt(channel, r"#\s*$|---- More", timeout_sec=15)
            print(out2, flush=True)
        finally:
            transport.close()
        return 0
    finally:
        db.close()
        print("done", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
