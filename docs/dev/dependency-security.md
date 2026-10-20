# Frontend dependency security

## 2026-10-20 remediation

The frontend lockfile updates address the 11 open GitHub dependency alerts
observed before this change:

- Next.js 16.3.8 resolves GHSA-vcvr-r3jv-pc5j, GHSA-2xp9-vwfh-vxw4,
  and GHSA-p293-qw3h-jr36.
- Vitest and its V8 coverage provider 4.1.11 resolve GHSA-5xrq-8626-4rwp
  and GHSA-82fw-gwwq-j7x9 (reported for Vitest and its mocker).
- Vite 6.4.4 resolves GHSA-fx2h-pf6j-xcff, GHSA-v6wh-96g9-6wx3,
  and GHSA-4w7w-66w2-5vf9.
- esbuild 0.25.12 resolves GHSA-67mh-4wv8-2f99.
- source-map-js 1.2.2 resolves GHSA-68fv-2mgg-jv7q.

Next.js stays on the 16.3 release line. Vite is explicitly constrained to the
patched version 6 line supported by the existing React plugin. GitLab retains
the two-worker test limit; Vitest 4 no longer accepts `--minWorkers`.

Local validation passed: 416 tests across 44 files with coverage, TypeScript
checking, and the standalone production build. Vitest 4 changes V8 coverage
remapping; the existing explicit source inclusion remains in place.

## Remaining upstream advisory

[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
affects braces through 3.0.3. At the time of this audit, 3.0.3 was the latest
published version and no patched version was available.

The dependency path is eslint-config-next → @next/eslint-plugin-next →
fast-glob → micromatch → braces. npm reports five high-severity entries for
this one root advisory and its dependents. This is a development dependency;
`npm ls --omit=dev braces` contains no installed package. The affected Next
lint helper processes root-directory globs supplied by local ESLint settings.
Troshka does not configure those globs.

Keep this finding open and update when an upstream fix is available. Do not
apply npm's suggested downgrade to Next.js 14 lint configuration to hide it.
No advisory suppression or dependency audit exclusion was added.
