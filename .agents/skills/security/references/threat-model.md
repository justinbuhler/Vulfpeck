# Threat model of a component

Read-only. Produce one document with these sections, each grounded in files or commands you ran.

1. **Surface.** Entry points (CLI flags, network listeners, hook inputs, files read from outside the repo, environment variables), trust boundaries, and the privileges each process runs with.
2. **Assets.** Secrets, credentials, data stores, and anything whose loss or change the owner would care about.
3. **Threats.** For each entry point, apply STRIDE (spoofing, tampering, repudiation, information disclosure, denial of service, elevation of privilege). Keep a threat only if a concrete attacker action reaches an asset.
4. **Supply chain.** Dependencies and their pinning (`uv.lock`, lockfiles), install-time scripts, CI actions pinned by sha or by tag, and downloaded binaries.
5. **Mitigations in place.** Existing guards, with the file that implements each and the test that proves it.
6. **Gaps.** Unmitigated threats, ranked by consequence, each with the smallest control that closes it.

Stop at the component boundary and queue anything outside it as a follow-up. A CRITICAL supply-chain finding blocks further work until the owner decides.
