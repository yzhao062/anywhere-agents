# Web Units Reference (Agy)

Read this when a unit needs the web.

## Web access

**Agy** runs on the user's local machine, so its requests leave from the user's local network
rather than the cloud fetcher's egress IP, often a residential IP. That can reach some pages a cloud
fetcher gets `403` on, though a hardened site can still block on bot score, fingerprint, or rate. It
also surfaces pages a cloud fetch would miss. The dispatcher's default mode grants the web and the
shell unattended, so a worker can fetch through `read_url` or a local-shell curl. Only `--mode plan`
withholds them (see `--mode` in SKILL.md and `PRUN_AGY_SANDBOX` in agy-quota-and-models.md); read a plan-mode result's
`Verification` and `Open items` fields before trusting it.

Web units, all on Agy:

- **Discover a page when the URL is unknown**: give the unit the question and let it search; ask it
  to list the candidate URLs it considered, so a thin search shows up in the result.
- **Fetch a known URL**: the unit fetches unattended through `read_url` or curl in the default mode.
- **A page that blocks the fetch**: have the unit retry through curl from the local network, and
  record which path failed and the HTTP status each returned.
- **A high-stakes fact that might be stale or blocked**: dispatch a second unit that verifies the
  claim from an independent source, and have the coordinator compare the two results.

An Agy web-fetch unit can use curl in the default mode (`--mode plan` denies it). Report the
HTTP status per URL so a cloud-vs-local block shows up in the result.

```bash
curl -sSL -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36" -o <body-file> -w "%{http_code} %{url_effective}\n" <URL>
```
