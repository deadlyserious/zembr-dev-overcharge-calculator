"""Write an arbitrary value straight to a project's custom field via
ScoroClient.modify() — bypassing the calc pipeline and the DRY_RUN gate
entirely. This is a deliberate, ad hoc write: it does NOT go through
handler.handler(), so none of handler.py's eligibility/period/rate logic
or DRY_RUN guard applies here. Use it to prove the write mechanism itself
works end-to-end against live Scoro, independent of whether the calc is
right.

Usage:
    export SCORO_API_KEY="..."
    export SCORO_COMPANY_ACCOUNT_ID="zembrpty"   # defaults to zembrpty if unset
    python3 write_arbitrary_value.py                       # writes 41103 to project 492
    python3 write_arbitrary_value.py 492 41103              # same, explicit
    python3 write_arbitrary_value.py 492 41103 c_overchargehours   # explicit field key

After running, this project's c_overchargehours will hold whatever value you
passed here, NOT a real computed overcharge — the next real run (dry or live)
will overwrite it with the actual calculated value.
"""

import os
import sys

from scoro_client import ScoroClient


def main():
    project_id = int(sys.argv[1]) if len(sys.argv) > 1 else 492
    value = float(sys.argv[2]) if len(sys.argv) > 2 else 41103
    field_key = sys.argv[3] if len(sys.argv) > 3 else "c_overchargehours"

    api_key = os.environ["SCORO_API_KEY"]
    company_account_id = os.environ.get("SCORO_COMPANY_ACCOUNT_ID", "zembrpty")

    client = ScoroClient(api_key, company_account_id)

    print(f"Writing {field_key}={value} to project {project_id} on {company_account_id}...")
    result = client.modify("projects", project_id, field_key, value)
    print(result)


if __name__ == "__main__":
    main()
