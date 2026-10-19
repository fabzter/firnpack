# Research: tarfile extraction-filter security and Python patch levels

> Compiled 2026-10-18 from web verification of python.org release pages,
> the Python devguide, PEPs, and CPython's tracker. This file records the
> evidence behind SPEC §5 (extraction-safety layers) and §18 (Python floor
> 3.13.16+). Verification levels are explicit in the Sources section —
> facts below were checked on the date shown; re-verify before relying on
> them after future Python releases.

## The three 2026 tarfile extraction-filter bypasses

| CVE | CPython issue | Mechanism | Fixed in |
|---|---|---|---|
| CVE-2026-82049 | gh-157190 | data/tar filter bypass via **hard links to symlinks** — hardlink resolution now uses the real target; escape outside destination possible pre-fix | 3.10.18, 3.11.13, 3.12.15, 3.13.16 (2026-09-30); **3.14+ NOT affected at all** — the 3.14.8 release page states 3.14 is not affected since 3.14.0b1 because `os.link()` follows symbolic links (gh-81793); 3.15.0 |
| CVE-2026-19672 | gh-155999 | extraction filter bypass allows **directory creation outside destination** | 3.12.15, 3.13.16 (2026-09-30); 3.14.8 (listed on its release page — status of 3.14.0–3.14.7 not established here); 3.15.0 |
| CVE-2026-87910 | gh-157265 (GHSA-RJ44-3777-MH5X) | **link-fallback ignores custom filter rejection (None)** — when the OS lacks link support, tarfile falls back to extracting the member and runs the filter twice; a None rejection could be ignored | 3.12.15, 3.13.16 (2026-09-30); **3.14.x unconfirmed — see below**; 3.15.0 (via RC3) |

**3.13.16 release page (python.org/downloads/release/python-31316/): lists
gh-157190 (CVE-2026-82049), gh-155999 (CVE-2026-19672), gh-157265
(CVE-2026-87910) under Security, and ships Windows installers** (64-bit
recommended, 32-bit, ARM64, embeddable — download index confirms). Released
2026-09-30 as the sixteenth and final maintenance release of 3.13 (future
3.13.x are security-only). This is the release that satisfies "patched AND
installer-available" — the basis of the owner's floor decision.
(Verification: python.org/downloads/release/python-31316/ fetched
directly 2026-10-19, confirming the 2026-09-30 release date, "last full
maintenance release" status, all three CVEs under Security, and Windows
installers; see Sources Tier 1.)

## Installer availability (Windows)

| Release | Date | Windows installer? |
|---|---|---|
| 3.12.10 | 2025-04-08 | **yes — the last 3.12.x with installers** |
| 3.12.11 … 3.12.15 | 2025-06 → 2026-09-30 | **no** — source-only security releases (3.12 entered security-fix-only mode) |
| 3.13.16 | 2026-09-30 | yes (release page lists them) |
| 3.14.8 | 2026-09-30 | yes; CVE-2026-19672 fixed, CVE-2026-82049 not applicable (3.14 never affected), **CVE-2026-87910 unconfirmed — see below** |
| 3.15.0 | 2026-10-09 | yes (current stable) |

Consequence: on Windows, a python.org 3.12 install can never reach 3.12.15
(the patched source-only release) — `py install 3.12` tops out at 3.12.10.

## 3.14.8 status re CVE-2026-87910 / gh-157265: UNCONFIRMED (do not clear without evidence)

**Scope**: this ambiguity is about CVE-2026-87910 **only**. CVE-2026-82049
never affected 3.14 (gh-81793), and CVE-2026-19672 is listed as fixed on the
3.14.8 page.

- The python.org **3.14.8** security list includes CVE-2026-19672 but **omits
  CVE-2026-87910 / gh-157265**.
- The 3.14 backport of the fix (**PR #157307**) closed **2026-10-01**, one day
  *after* the 3.14.8 release (2026-09-30).
- Wolfi (chainguard) ships a patched **3.14.8 rebuild dated 2026-10-08**
  (SNYK/wolfi data) — consistent with plain 3.14.8 having shipped without the
  fix.
- Counter-signal (why not "false"): the Python Insider post of 2026-10-14
  announcing 3.10.22/3.11.17/3.12.15/3.13.16/3.14.8 mentions CVE-2026-87910 in
  its family-wide summary, which does not disambiguate which releases carry it.
- **Resolution rule**: treat all 3.14.x as unpatched for gh-157265 until a
  3.14.x release page or changelog explicitly lists it.

## 3.15.0 status re gh-157265: CONFIRMED patched

- gh-157265 appears under **RC3 Security** in the 3.15.0 changelog (RC3
  released 2026-10-02); the final-release notes state "no new changes" vs
  RC3. 3.15.0 released 2026-10-09.
- The 3.15 branch also carries an earlier hardlink-fallback escape fix
  (commit 672825e, gh-151558/gh-151559) — corroborated only via search
  summaries, not diffed in this session.

## PEP 706 filter availability (context for SPEC §5 layer 1)

- The `data` extraction filter shipped natively in 3.12 and was
  **security-backported to 3.10.12+ / 3.11.4+** (PEP 706; PEP 721 states the
  backport reached all then-supported Pythons; pypa/pip#14241 removed pip's
  own no-filter fallback on that basis).
- Detection is `hasattr(tarfile, 'data_filter')`. **The filter's presence
  says nothing about the 2026 bypass CVEs** — those are bugs *in* the filter,
  fixed only at the patch levels tabulated above. Hence SPEC §5 layer 4's
  per-minor version table instead of a hasattr gate.

## Owner decision recorded from this evidence

2026-10-18, owner: **floor = 3.13.16+** (`requires-python >= 3.13.16`) —
first release both fully patched (all three CVEs listed on its release page)
and shipped with Windows installers. Q19: warn-and-proceed below
known-patched levels (distro builds carry fixes without bumping version
numbers; pip's floor gate handles python.org installs).

## Sources (checked via web search 2026-10-18 — verification levels differ)

**Tier 1 — release-page content quoted directly into this session's search
results (quoteable):**

- python.org **3.14.8** release page: security list (includes
  CVE-2026-19672; omits CVE-2026-87910) and the explicit note "Python 3.14 is
  not affected by CVE-2026-82049 … since Python 3.14.0b1, os.link() follows
  symbolic links (gh-81793)"
- python.org **3.12.15** release page: security-only, source-only, release
  date 2026-09-30, CVE list
- python.org **3.12.11** release page: security-only, no Windows installers
  for 3.12.x beyond 3.12.10
- python.org **3.12.10** release page: final full-maintenance release with
  installers, 2025-04-08
- **3.15.0** changelog / what's-new: gh-157265 under RC3 Security; "no new
  changes" in final
- devguide.python.org/versions: EOL dates (3.10 EOL 2026-10-01; 3.11
  security-only to 2027-10; 3.15 current stable)
- Python Insider 2026-10-14: "Python 3.10.22, 3.11.17, 3.12.15, 3.13.16 and
  3.14.8 are now available"
- python.org **3.13.16** release page: Security list (all three CVEs, gh
  refs) + Windows installers — fetched 2026-10-18 (this session, post
  keystone verification)

**Tier 2 — corroborated via search-result summaries or third-party
trackers; page/commit NOT re-inspected in this session (re-check before
shipping in README/docs):**

- 3.10.18 / 3.11.13 as the fix levels for the older branches
- cpython PRs #157306 (3.11 backport), #157307 (3.14 backport), #157309
  (3.15); commit 672825e on 3.15
- GHSA-RJ44-3777-MH5X / CVE-2026-87910 entries (vulnerability.circl.lu,
  cvefeed.io), NVD CVE-2026-19672, Snyk wolfi entry (patched
  3.14.8_git20261008-r0)
- pypa/pip issue #14241 (filter-backport rationale)
