# gitlab-quay-mirror

Keep this repository limited to a reviewed image catalog, tagged registry copies and offline transfer/import. Production jobs need Python and skopeo, not Docker or privileged execution.

Read `.agent/PENDING.md` and `.agent/SKILL.md` when present. Never commit `.agent/`, credentials, state or image bundles.

Validate changes with `python3 -m unittest discover -s tests -v`, `ruff check mirror.py tests`, and YAML lint on both GitLab configurations and the GitHub workflow. A catalog validation or CI lint is not an executed transfer. `tests/matrix.py <image:tag>...` runs real upstream references end to end.

`E2E_FIXTURES=true python3 tests/e2e.py` exercises actual registry writes. Log in to dedicated test registries with skopeo, set LOW_QUAY_HOST and HIGH_QUAY_HOST, and a disposable E2E_ORG; never point this test at a production application repository. Its fixture repositories are uniquely named and retained for the registry's retention policy.

Keep the README under 100 lines and QUICKSTART under 40. Keep detailed architecture and verification evidence in `docs/reference/`. Check the actual OCI digest graph before any high-side push; a valid tar checksum alone is insufficient.
