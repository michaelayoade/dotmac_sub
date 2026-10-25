# Device transcript corpus

Captured CLI output from real devices, used as parser regression input.
No device is contacted by the tests; they only read these files.

Layout: `tests/transcripts/huawei/<model>/<command>.txt` plus a sibling
`<command>.expected.json` golden. `<model>` is the OLT model (`MA5608T`,
`MA5800-X2`); `<command>` is the command with non-alphanumerics replaced by `_`.

Transcripts keep the device's exact bytes, including trailing whitespace, ANSI
redraws and `---- More ----` pager markers, so `tests/transcripts/` is excluded
from the `trailing-whitespace` pre-commit hook.

## Anonymisation rule

No customer names and no addresses anywhere, including the `Description`
column. Replace them with placeholders such as `CUSTOMER_zone_Zone_1_descr`.
ONT serials are allowed. Estate management IPs and credentials must not
appear (use documentation ranges if an address is unavoidable).

## Adding a capture

1. Save the raw output as `<model>/<command>.txt`, then anonymise it.
2. Register a parser for `<command>` in `PARSERS` in
   `tests/test_huawei_transcript_corpus.py` (a classifier is fine when no
   parser exists yet).
3. Run `UPDATE_TRANSCRIPT_GOLDENS=1 pytest tests/test_huawei_transcript_corpus.py`
   to write the golden, and review the JSON by hand before committing.

## Notes on current contents

- `MA5800-X2/display_ont_info_port_all.txt`: Garki, port 0/2/1 (16 ONTs).
- `MA5608T/display_ont_info_by_sn.txt`: BOI, with a pager marker mid-record.
  There is no dedicated by-sn parser yet (`find_ont_by_serial` uses inline
  regexes); the golden pins the response classifier and the generic key/value
  detail parser, for the future package.
- `*/screen_length_0_temporary_rejected.txt`: both models reject the Huawei
  switch command `screen-length 0 temporary` with `% Unknown command`. These
  two are reconstructed from the production incident text (echo
  `screen-length0temporary` plus the error line), not raw captures. Replace
  them with raw captures when available.

## Paging-off status per model (design s7)

`OltSshPolicy.paging_off_command` is `None` for MA5608T, MA5800 and MA5600.
No paging-off command is confirmed in Knowledge or the repo, so nothing is sent
and the pager is answered instead. Candidates (`scroll`,
`mmi-mode original-output`) are UNCONFIRMED: test on the lab OLT or BOI, add
the capture here, then set the value on that model's policy.
