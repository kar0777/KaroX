# DSH RC Dependency Install — Mission 09

**Date:** 2026-11-06
**Repository:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win`
**Mission:** controlled PyPI dependency installation and clean-venv CLI validation.
**Verdict:**

```text
CLEAN_VENV_DEPENDENCY_INSTALL PASS
CLEAN_VENV_CLI_SMOKE PASS
OFFLINE_RC_CANDIDATE — ADVANCED
```

`RELEASE_READY` is not claimed. Live integrations, browser/desktop/3D smoke,
full pytest and cross-OS CI remain outside this mission.

## A. Git reality

The mission preconditions were checked before any mutation.

| Check | Expected | Measured |
|---|---|---|
| `git rev-parse --show-toplevel` | `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\win` | `D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win` (same path) |
| Starting branch | `audit/dsh-rc-deps-headless-20261106` | `audit/dsh-rc-deps-headless-20261106` |
| Starting HEAD | `1ac42c88001bca3f84386511d5d582b2c17cd994` | `1ac42c88001bca3f84386511d5d582b2c17cd994` |
| `git status --porcelain` | empty | empty |
| Worktrees | nine, including `b/win` | nine, including `b/win` |

Initial `git log -5 --oneline --decorate` (before branch creation):

```text
1ac42c8 (HEAD -> audit/dsh-rc-deps-headless-20261106) docs: DSH mission 08 offline dependency and headless preflight audit (2026-11-06)
cf0482f tests: pin a browser-free quick gate and real package data
10ac717 (audit/dsh-rc-offline-20261106) docs: DSH mission 07 offline RC audit (2026-11-06)
428dcac (audit/dsh-release-gates-20261106) docs: DSH mission 06 honest release-gates audit (2026-11-06)
e55a22a build: unify package size gate and remove unused imports
```

The new branch `audit/dsh-rc-deps-install-20261106` was created at the
unchanged starting HEAD:

```text
audit/dsh-rc-deps-install-20261106
1ac42c88001bca3f84386511d5d582b2c17cd994
```

The source-and-dependency baseline was also verified:

```text
git diff 10ac717..HEAD -- src pyproject.toml  -> empty, exit 0
```

The first and last worktree snapshots agree on branch and HEAD for all eight other worktrees:

| Worktree | Branch | HEAD | Status |
|---|---|---|---|
| `b/base` | `race/b-base` | `4d8d5d975d5fbd3f23540930017dd804fb7e38b3` | clean |
| `b/browser` | `race/b-browser` | `b4dab3ae0bb3c747ba4f1ce6aa4a5ea13a0e203d` | clean |
| `b/desktop` | `race/b-desktop` | `09a9320b4560307e1733248a3426863aa9db3214` | clean |
| `b/integrate` | `v6/sbx-improve-20261105` | `aa5b590dda4919df03341effba82880642af7b32` | clean |
| `b/kernel` | `race/b-kernel` | `53038ac8bda49b62a6fa5970f12734801805e3ae` | clean |
| `b/mcp` | `race/b-mcp` | `79e3b4477dc3be1f06e03433b9dd9e0787958c4d` | clean |
| `b/skills` | `race/b-skills` | `c00237ef02f43f0615e0899239abe42a7a6ccc55` | clean |
| `b/tui` | `race/b-tui` | `47e7ed223cc4c7c1997bcd5faa4e0ed13eb8e5f2` | clean |
| `b/win` | `audit/dsh-rc-deps-install-20261106` | `1ac42c88001bca3f84386511d5d582b2c17cd994` | clean |

No mutating command was run inside the other eight worktrees; only a read-only
`git status` snapshot was taken. Their branch names and HEADs match the initial
snapshot, and all eight were clean at the final check.

**Final HEAD before the report commit:** `1ac42c88001bca3f84386511d5d582b2c17cd994`
(identical to the starting HEAD). The report commit is the only new commit; its
full hash is recorded in the completion message. After that commit the branch
HEAD is the report commit.

### Repository-guidance conflict

`AGENTS.md` contains a generic instruction to confirm the winner tree
`b\integrate`. Mission 08 established `b\win` as the active audit worktree.
Mission 09's explicit expected root, branch and HEAD were followed; the audit
did **not** switch to `b\integrate`. `AGENTS.md` was not modified. This is a
documentation conflict, not a Git-state change.

## B. Wheel evidence

### B.1 Source archive and build

The release source was extracted with `git archive HEAD` into the isolated
temporary directory, not copied from the live worktree:

```text
git archive --format=tar --output <TEMP>\archive\karox-src-1ac42c8.tar HEAD
tar -xf <TEMP>\archive\karox-src-1ac42c8.tar -C <TEMP>\source
```

Archive size: `6,021,120` bytes.
Archive SHA-256: `267b60f16e3cc96a849a681d9c695e846f19cae1fcfa8127c0d4c769d77a8ed0`.

Build command:

```text
python -m build --wheel --no-isolation --outdir <TEMP>\dist <TEMP>\source
```

The build used only the locally available toolchain; no build-system dependency
was downloaded.

| Item | Measured |
|---|---|
| Python | `3.13.11` (CPython, MSC v.1944 64-bit AMD64) |
| `build` | `1.5.0` |
| `setuptools` | `80.3.1` |
| `wheel` | `0.45.1` |
| Build exit code | `0` |
| Build duration | `5.91 s` |
| Wheel filename | `karox_runtime-6.0.0a3-py3-none-any.whl` |
| Wheel size | `1,142,778` bytes |
| Wheel SHA-256 | `54d76cb3f27e783b36e3621aa1983413365d63531cf205b91262c6005b1e603e` |

No byte-for-byte wheel reproducibility is claimed: archive/build timestamps can
change the ZIP metadata without changing the product source.

### B.2 Independent metadata and content inspection

The wheel was inspected independently with `zipfile` and `email.parser`; the
inspection JSON is at `<TEMP>\manifests\wheel-inspection.json`.

| Metadata / content | Measured |
|---|---|
| Name | `karox-runtime` |
| Version | `6.0.0a3` |
| `Requires-Python` | `>=3.10` |
| Console entry point | `karox = karox.entrypoint:main` |
| Wheel entries | `183` |
| Python modules | `162 / 162` |
| Built-in skills | `8 / 8` |
| TUI theme | `karox/tui/theme.tcss` |
| Prompt resource | `karox/prompts/visual_checker.md` |
| Data resource | `karox/data/models_dev.json.gz`, `karox/data/NOTICE.md` |
| Engine markers | `karox/engine/LICENSE.md`, `karox/engine/NOTICE.md`, `karox/engine/py.typed` |
| Tests / caches / `.pyc` / `.egg-info` | `0` forbidden entries |
| RECORD rows | `183` |
| RECORD hash/size mismatches | `0` |
| RECORD unlisted/extra entries | `0` |

Runtime `Requires-Dist` entries (19):

```text
anyio<4.15,>=4.4
httpx<1.0,>=0.27
keyring<27,>=25
SecretStorage<4,>=3.3; sys_platform == "linux"
jeepney<1,>=0.8; sys_platform == "linux"
mcp<2.0,>=1.27
starlette<2.0,>=0.37
uvicorn<1.0,>=0.30
psutil<8.0,>=5.9
playwright<2.0,>=1.50
pydantic<3.0,>=2.7
PyYAML<7.0,>=6.0
textual<7.0,>=0.89
rich<15.0,>=13.7
Pygments<3.0,>=2.17
markdown-it-py<5.0,>=3.0
python-dotenv<2.0,>=1.0
platformdirs<5.0,>=4.0
Jinja2<4.0,>=3.1
```

The four `dev` entries were present in metadata and were deliberately excluded
from installation:

```text
pytest<9.1,>=8.0; extra == "dev"
pytest-asyncio<1.4,>=0.26; extra == "dev"
ruff<1.0,>=0.14; extra == "dev"
mypy<2.0,>=1.13; extra == "dev"
```

`httpx<1.0,>=0.27` is present exactly as expected. Package-data completeness
matches Mission 08: 183 entries, 162/162 modules, 8/8 skills, theme and prompt
resources, engine LICENSE/NOTICE/`py.typed`, and no tests, caches or retired
modules. `scripts/check_tree.py` independently reports 162 modules and no
legacy surface.

## C. Dependency closure

### C.1 Download

The locally built wheel was the only dependency source of truth:

```text
python -m pip --isolated --disable-pip-version-check --no-cache-dir download ^
  --only-binary=:all: --index-url https://pypi.org/simple ^
  --retries 2 --timeout 20 --progress-bar off ^
  --dest <TEMP>\wheelhouse <TEMP>\dist\karox_runtime-6.0.0a3-py3-none-any.whl
```

Download exit code: `0`; duration: `9.44 s`. No `--no-deps`, no `dev` extra,
no version relaxation, no source distributions, no extra index, no
`--trusted-host`, and no TLS-verification override were used.

A second, non-installing resolution was run only to capture official URLs and
hashes:

```text
python -m pip --isolated --disable-pip-version-check --no-cache-dir install ^
  --dry-run --ignore-installed --only-binary=:all: ^
  --index-url https://pypi.org/simple --report <TEMP>\manifests\pypi-resolution-report.json ^
  <TEMP>\dist\karox_runtime-6.0.0a3-py3-none-any.whl
```

That command exited `0` in `6.50 s` and reported 52 install entries (the local
KaroX wheel plus 51 dependencies). It did not install anything. Every dependency
entry's `download_info.url` uses `https://files.pythonhosted.org`; the only
other URL is the `file:///` URL of the locally built KaroX wheel. The report's
SHA-256 values match the artifacts byte-for-byte (0 mismatches).

### C.2 Counts and limits

| Item | Measured |
|---|---|
| Direct runtime dependencies declared | `19` |
| Direct runtime dependencies applicable on Windows/Python 3.13 | `17` |
| Direct runtime dependencies skipped by `sys_platform == "linux"` | `2` (`SecretStorage`, `jeepney`) |
| Transitive packages | `34` |
| Total dependency packages | `51` |
| Wheelhouse files | `52` (51 dependency wheels + local KaroX wheel copied by pip) |
| Dependency wheel bytes | `57,745,653` |
| Total wheelhouse bytes | `58,888,431` |
| Non-wheel artifacts | `0` |
| Source distributions | `0` |
| `dev` packages present | `0` (`pytest`, `pytest-asyncio`, `ruff`, `mypy` absent) |
| Limits | `52 < 100` files; `58,888,431 < 1 GiB`; `9.44 s < 20 min` |

Pip also requested two dependency extras, both correctly part of the runtime
closure and neither the `dev` extra:

- `crypto` via `mcp -> pyjwt[crypto]` (brings `cryptography`, `cffi`, `pycparser`);
- `linkify` via `textual -> markdown-it-py[linkify]` (brings `linkify-it-py`).

The 51 dependency wheels are all binary wheels. The Windows-specific
`pywin32`/`pywin32-ctypes` closure is included; the Linux-only
`SecretStorage`/`jeepney` entries are not applicable on this target.

The machine-readable manifest is `<TEMP>\manifests\dependency-manifest.json`
(also `pip-list.json`, `pypi-resolution-report.json`, and
`provider-presets-parse.json`). Nothing from the temporary directory was copied
into the repository.

### C.3 Artifact table

| Package | Version | Wheel filename | Size | SHA-256 |
|---|---|---|---:|---|
| annotated-types | 0.8.0 | `annotated_types-0.8.0-py3-none-any.whl` | 13,427 | `f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0` |
| anyio | 4.14.2 | `anyio-4.14.2-py3-none-any.whl` | 125,813 | `9f505dda5ac9f0c8309b5e8bd445a8c2bf7246f3ce950121e45ea15bc41d1494` |
| attrs | 26.1.0 | `attrs-26.1.0-py3-none-any.whl` | 67,548 | `c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309` |
| certifi | 2026.10.23 | `certifi-2026.10.23-py3-none-any.whl` | 135,082 | `e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1` |
| cffi | 2.1.1 | `cffi-2.1.1-cp313-cp313-win_amd64.whl` | 185,688 | `1aa5645c30469b09530c4ebca77ebf8f17618293c58f8549cb1a543a50236e7d` |
| click | 8.5.1 | `click-8.5.1-py3-none-any.whl` | 126,163 | `8bb5376d2006950386f8c6427613334a2acadc5f559cb80e638d00b4df957e37` |
| cryptography | 50.0.2 | `cryptography-50.0.2-cp311-abi3-win_amd64.whl` | 3,824,692 | `7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c` |
| greenlet | 3.5.6 | `greenlet-3.5.6-cp313-cp313-win_amd64.whl` | 324,086 | `5b602b4201b965a8354d74e232364a66ff243dd142e350d035f46169bb36e13d` |
| h11 | 0.16.0 | `h11-0.16.0-py3-none-any.whl` | 37,515 | `63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86` |
| httpcore | 1.0.9 | `httpcore-1.0.9-py3-none-any.whl` | 78,784 | `2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55` |
| httpx | 0.28.1 | `httpx-0.28.1-py3-none-any.whl` | 73,517 | `d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad` |
| httpx-sse | 0.4.3 | `httpx_sse-0.4.3-py3-none-any.whl` | 8,960 | `0ac1c9fe3c0afad2e0ebb25a934a59f4c7823b60792691f779fad2c5568830fc` |
| idna | 3.20 | `idna-3.20-py3-none-any.whl` | 69,583 | `ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c` |
| jaraco.classes | 3.4.0 | `jaraco.classes-3.4.0-py3-none-any.whl` | 6,777 | `f662826b6bed8cace05e7ff873ce0f9283b5c924470fe664fff1c2f00f581790` |
| jaraco.context | 6.1.2 | `jaraco_context-6.1.2-py3-none-any.whl` | 7,871 | `bf8150b79a2d5d91ae48629d8b427a8f7ba0e1097dd6202a9059f29a36379535` |
| jaraco.functools | 4.6.0 | `jaraco_functools-4.6.0-py3-none-any.whl` | 11,677 | `99e3dc0060c5cbe8fcd1cdb36258e2a65ca40f1566b2033b12abb1bb44dd3c30` |
| Jinja2 | 3.1.6 | `jinja2-3.1.6-py3-none-any.whl` | 134,899 | `85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67` |
| jsonschema | 4.26.0 | `jsonschema-4.26.0-py3-none-any.whl` | 90,630 | `d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce` |
| jsonschema-specifications | 2025.9.1 | `jsonschema_specifications-2025.9.1-py3-none-any.whl` | 18,437 | `98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe` |
| keyring | 25.7.0 | `keyring-25.7.0-py3-none-any.whl` | 39,160 | `be4a0b195f149690c166e850609a477c532ddbfbaed96a404d4e43f8d5e2689f` |
| linkify-it-py | 2.2.0 | `linkify_it_py-2.2.0-py3-none-any.whl` | 21,971 | `3adc40eb5af300b2605fcfdb968c24e1d780a90f1f2221af7c15e5111e94d443` |
| markdown-it-py | 4.2.0 | `markdown_it_py-4.2.0-py3-none-any.whl` | 91,687 | `9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a` |
| MarkupSafe | 3.0.4 | `markupsafe-3.0.4-cp313-cp313-win_amd64.whl` | 14,313 | `1f1f9477e174582b0a1b583d60b66e1f2cf5d3fe12cee985e4aedf44766600e5` |
| mcp | 1.30.0 | `mcp-1.30.0-py3-none-any.whl` | 234,581 | `666edb5009503e1047c9d60346a756f94b261f05cc2625f23d41c728ffc484d0` |
| mdit-py-plugins | 0.6.1 | `mdit_py_plugins-0.6.1-py3-none-any.whl` | 66,663 | `214c82fb2ac524472ab6a5bcab1de80f73b50443e187f401bfd77efbc7c6481d` |
| mdurl | 0.1.2 | `mdurl-0.1.2-py3-none-any.whl` | 9,979 | `84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8` |
| more-itertools | 11.2.1 | `more_itertools-11.2.1-py3-none-any.whl` | 74,405 | `35a7377edd1dd6608dcb2cdf534ded55ea32d49448875ad042cd3f879fb1ded0` |
| platformdirs | 4.14.5 | `platformdirs-4.14.5-py3-none-any.whl` | 35,402 | `8e778c6fa421516f4d3369449f8df3b0e1b72c0e0005fe88e3ec772d0a14e750` |
| playwright | 1.64.1 | `playwright-1.64.1-py3-none-win_amd64.whl` | 38,801,836 | `5e240a79464434c9260957b9ef59a5808e02e96e3fcabdc717fd7485260b9730` |
| psutil | 7.2.2 | `psutil-7.2.2-cp37-abi3-win_amd64.whl` | 137,737 | `eb7e81434c8d223ec4a219b5fc1c47d0417b12be7ea866e24fb5ad6e84b3d988` |
| pycparser | 3.12 | `pycparser-3.12-py3-none-any.whl` | 62,600 | `a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e` |
| pydantic | 2.14.3 | `pydantic-2.14.3-py3-none-any.whl` | 494,071 | `3f7b347782ab7e4cf4c798496ae3a82787d9f21ec12676a9c564355eeef8daa8` |
| pydantic-settings | 2.16.2 | `pydantic_settings-2.16.2-py3-none-any.whl` | 81,994 | `8d523e5c129b097686bde019bd6109bff53f4c8c3f63f1ddf9933ffa42e5ab4f` |
| pydantic_core | 2.50.4 | `pydantic_core-2.50.4-cp313-cp313-win_amd64.whl` | 2,262,887 | `4c29b8f2d6417069bb1f257e6b9113f5d64dc2f554cc200ea90b4fe2c5cdb733` |
| pyee | 14.0.0 | `pyee-14.0.0-py3-none-any.whl` | 15,553 | `3ac2d3229a9677f7de2c33d7f52fe25b638a46b19c413fea2edc8c6d0a644e4d` |
| Pygments | 2.21.0 | `pygments-2.21.0-py3-none-any.whl` | 1,250,147 | `2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9` |
| PyJWT | 2.15.1 | `pyjwt-2.15.1-py3-none-any.whl` | 33,860 | `42d59d631f7768a1028a64c7ff581a9bf7519804daf91fc5b6c56e30eec5e193` |
| python-dotenv | 1.2.4 | `python_dotenv-1.2.4-py3-none-any.whl` | 23,266 | `42269a8a5b3fd54ffa6f3d84b18abed50064717576b4ecf03dc4a55d8aa04fdc` |
| python-multipart | 0.0.33 | `python_multipart-0.0.33-py3-none-any.whl` | 32,253 | `8f16c07b36add9da6b2ee87d3efcaf0a45a6de9c3d0a5dd94e9ccf7675a7bc45` |
| pywin32 | 312 | `pywin32-312-cp313-cp313-win_amd64.whl` | 6,914,298 | `c53e878d15a1c44788082bfe712a905433473aa38f86375b7cf8b45e3acbaaf9` |
| pywin32-ctypes | 0.2.3 | `pywin32_ctypes-0.2.3-py3-none-any.whl` | 30,756 | `8a1513379d709975552d202d942d9837758905c8d01eb82b8bcc30918929e7b8` |
| PyYAML | 6.0.3 | `pyyaml-6.0.3-cp313-cp313-win_amd64.whl` | 154,090 | `79005a0d97d5ddabfeeea4cf676af11e647e41d81c9a7722a193022accdb6b7c` |
| referencing | 0.37.0 | `referencing-0.37.0-py3-none-any.whl` | 26,766 | `381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231` |
| rich | 14.3.4 | `rich-14.3.4-py3-none-any.whl` | 310,480 | `07e7adb4690f68864777b1450859253bed81a99a31ac321ac1817b2313558952` |
| rpds-py | 2026.9.1 | `rpds_py-2026.9.1-cp313-cp313-win_amd64.whl` | 222,409 | `3e93b2cd69a9830be33e03945cd7cda940a0a8bfcfbff41d6144f0cb0d3d8bd9` |
| sse-starlette | 3.6.0 | `sse_starlette-3.6.0-py3-none-any.whl` | 17,985 | `e4dfa4d237d2712cb2b4d4918eda9b457cd6b44bbecdc838af75de836565bafd` |
| starlette | 1.10.0 | `starlette-1.10.0-py3-none-any.whl` | 80,571 | `478c0ab3b4ced6e82719849c118fa406f0278a3499c1a898cd15cd31bdc38ce4` |
| textual | 6.12.0 | `textual-6.12.0-py3-none-any.whl` | 714,912 | `cf9ea9a54d213c7736efe9fef440c7f49218d4e6ab75279afd060eded9c567ec` |
| typing-inspection | 0.4.5 | `typing_inspection-0.4.5-py3-none-any.whl` | 14,874 | `bc210535442b39e1bf2805e0c7a17dfd6a4ace760c84c7f7d16245d23a6b1c39` |
| typing_extensions | 4.16.0 | `typing_extensions-4.16.0-py3-none-any.whl` | 45,571 | `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8` |
| uvicorn | 0.54.0 | `uvicorn-0.54.0-py3-none-any.whl` | 87,427 | `505bdb0f318731d45f1f712071fc781a8981f6847a31c902c9f5e652d4f67faf` |

Local artifact (not a PyPI download):

| Package | Version | Wheel filename | Size | SHA-256 |
|---|---|---|---:|---|
| karox-runtime | 6.0.0a3 | `karox_runtime-6.0.0a3-py3-none-any.whl` | 1,142,778 | `54d76cb3f27e783b36e3621aa1983413365d63531cf205b91262c6005b1e603e` |

## D. Clean installation

A fresh venv was created from the temporary root (outside the repository), not
from the source checkout:

```text
Set-Location <TEMP>
python -m venv <TEMP>\venv-clean
```

| Check | Measured |
|---|---|
| Venv creation exit | `0` |
| Venv creation duration | `5.25 s` |
| Python | `3.13.11` |
| Venv pip | `25.3` |
| `include-system-site-packages` | `false` |
| `PYTHONPATH` at creation/install | not set |
| Creation/install cwd | `<TEMP>` (outside every Git worktree) |

Offline installation command:

```text
<TEMP>\venv-clean\Scripts\python.exe -m pip --isolated ^
  --disable-pip-version-check --no-cache-dir install ^
  --no-index --find-links <TEMP>\wheelhouse ^
  <TEMP>\dist\karox_runtime-6.0.0a3-py3-none-any.whl
```

`--no-index` was explicitly present. The install log contains `0` `http(s)://`
URLs, `0` `Downloading` lines and `0` `Looking in indexes` lines; every
dependency was processed from the local wheelhouse, and the explicitly named
KaroX wheel came from `<TEMP>\dist`.

| Result | Measured |
|---|---|
| Install exit code | `0` |
| Install duration | `23.79 s` |
| Packages in venv | `53` (`51` dependencies + `karox-runtime` + `pip`) |
| `pip check` exit code | `0` |
| `pip check` output | `No broken requirements found.` |
| Installed version | `6.0.0a3` |
| Import path | `<TEMP>\venv-clean\Lib\site-packages\karox\__init__.py` |
| `sys.prefix` | `<TEMP>\venv-clean` |
| `sys.base_prefix` | `C:\Users\ekono\AppData\Local\Programs\Python\Python313` |
| `site.ENABLE_USER_SITE` under `-I` | `false` |
| Repository path on `sys.path` | `false` |

`direct_url.json` records the exact local artifact and hash:

```json
{"archive_info": {"hash": "sha256=54d76cb3f27e783b36e3621aa1983413365d63531cf205b91262c6005b1e603e", "hashes": {"sha256": "54d76cb3f27e783b36e3621aa1983413365d63531cf205b91262c6005b1e603e"}}, "url": "file:///C:/Users/ekono/AppData/Local/Temp/karox-mission09-20261106/dist/karox_runtime-6.0.0a3-py3-none-any.whl"}
```

The system Python and its site-packages were not used by the installed package.
The venv's `include-system-site-packages` is false, the import proof resolves
`karox`, `httpx`, `textual` and `mcp` inside the venv, and no repository path
appears on `sys.path`.

## E. CLI smoke

All commands were executed with the installed venv executable, from
`<TEMP>\cli-work` (outside the repository), with isolated state:

```text
KAROX_CONFIG_DIR=<TEMP>\config
KAROX_RUNTIME_DIR=<TEMP>\runtime
```

| Command | Exit | Duration | Result |
|---|---:|---:|---|
| `karox --version` | `0` | `1.13 s` | `karox 6.0.0a3` |
| `karox paths` | `0` | `1.04 s` | config/runtime/session paths under `<TEMP>` |
| `karox provider presets` | `0` | `1.01 s` | 21 JSON Lines objects |
| `karox --help` | `0` | `1.01 s` | usage and command list |
| `karox agent run --help` | `0` | `1.02 s` | usage and options |

No command wrote to `<TEMP>\config` or `<TEMP>\runtime`; both remained empty.
No provider credentials were configured. No agent task, model call, TUI,
bridge, browser, OAuth or MCP live operation was started.

### E.1 `provider presets` JSON Lines validation

The output is one JSON object per line, not a single JSON array:

- first non-whitespace character: `{` (not `[`);
- non-empty lines: `21`;
- valid JSON objects: `21`;
- parse errors: `0`;
- unique `preset_id` values: `21`;
- every object has a string `preset_id`.

Preset IDs:

```text
routing-run, omniakey, apimaster, chutes, empiriolabs, puter, tinfoil,
vivgrid, merge-gateway, openrouter, openai-compatible,
anthropic-compatible, local-openai, openai, anthropic, gemini,
yandex-ai-studio, gigachat, stepfun, stepfun-plan, omniroute
```

Mission 08's supplementary smoke observed 21 provider preset objects. This
clean-dependency-install run also observed 21, with the same IDs; there is no
count difference to explain. The Mission 08 observation was supplementary
(`--system-site-packages`, `--no-deps`) and did not prove dependency-complete
installation. This mission replaces it with a genuinely fresh venv, full
runtime dependencies, offline install and `pip check`.

Portability note: when stdout is redirected, the default Windows console
encoding in this environment is `cp1251`, so the raw bytes are not strict UTF-8
(byte `0xd1` at offset 185). Decoding with `cp1251` yields 21/21 valid objects
and zero parse errors. A supplemental run with `PYTHONIOENCODING=utf-8`
produced valid UTF-8 and again 21/21 objects. This is an output-encoding
portability caveat, not a CLI command failure.

## F. Safety and regression checks

### F.1 Safe regression checks

The existing development environment already contained pytest, the required
test imports, ruff and mypy; nothing was installed to run these checks.

| Check | Result |
|---|---|
| Documented 12-file quick gate | `64 passed`, exit `0`, `3.26 s` internal / `4.46 s` wall |
| `python scripts/check_tree.py` | `162` modules, `86,996` lines, no legacy surface, exit `0` |
| `python -m ruff check src tests scripts --no-cache` | all checks passed, exit `0` |
| `python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui` | success, 13 source files, exit `0` (one informational unused-config-section note) |
| `git diff --check` | exit `0` |

Only the documented browser-free 12-file allowlist was run. No full pytest, no
`tests/test_engine_tools.py`, no browser/computer-use tests and no live tests
were run.

### F.2 Safety record

- **Network:** only official PyPI. Two pip operations used
  `--index-url https://pypi.org/simple`: the dependency download and the
  non-installing resolution report. All 51 dependency URLs use
  `files.pythonhosted.org`; no third-party mirror, extra index, `--trusted-host`
  or TLS-verification override was used.
- **Other external network destinations:** `0` observed or configured.
- **System/global installations:** `0`. All installs targeted the temporary
  venv; no `--user`, no system pip install, no global pip configuration change.
- **Browser / Chromium / Playwright launches:** `0`.
- **Browser binary installations:** `0`. Only the Playwright Python wheel was
  installed; `playwright install` was never run.
- **Paid API/model calls:** `0`.
- **Provider credentials accessed:** `0`. No `.env`, keyring, provider token or
  environment dump was read; no credential command was run.
- **Bridge/OAuth/MCP live calls:** `0`.
- **Other worktrees changed:** `0`. All eight other worktrees retain their
  original branches and HEADs; all eight were clean at the final check.
- **Push/merge/rebase/reset/tag/publish:** `0`.
- **Source/test/dependency changes:** `0`. The only repository change is this
  audit report.

## G. Residual blockers

This is not a release-ready claim. The following remain outside this mission:

- Live provider and TUI smoke.
- Bridge/OAuth live validation.
- External MCP live validation.
- Browser/desktop/3D live validation; Playwright browser binaries are not
  installed.
- Full pytest.
- Cross-OS CI.
- Linux/macOS dependency closure. The closure proven here is specific to
  CPython 3.13.11 on Windows 11 AMD64; Linux would additionally require the
  `SecretStorage`/`jeepney` branch and was not downloaded or installed.
- The locale-encoded JSONL caveat above for consumers that assume UTF-8 on
  redirected stdout.

## H. Machine-readable YAML

```yaml
mission: dsh-rc-dependency-install-20261106
date: 2026-11-06
repository: D:/проекты/KaroX-v5/lab/kernel-duel-20261025/b/win
starting_branch: audit/dsh-rc-deps-headless-20261106
starting_head: 1ac42c88001bca3f84386511d5d582b2c17cd994
branch: audit/dsh-rc-deps-install-20261106
final_head_before_report_commit: 1ac42c88001bca3f84386511d5d582b2c17cd994
worktree_clean_before_report: true
git_preconditions: PASS
source_diff_10ac717_head_src_pyproject: PASS

python_version: 3.13.11
python_implementation: CPython
platform: Windows-11-10.0.26300-SP0
machine: AMD64
sys_platform: win32

wheel_build: PASS
wheel_filename: karox_runtime-6.0.0a3-py3-none-any.whl
wheel_size_bytes: 1142778
wheel_sha256: 54d76cb3f27e783b36e3621aa1983413365d63531cf205b91262c6005b1e603e
wheel_entries: 183
wheel_python_modules: 162
wheel_builtin_skills: 8
wheel_record_ok: true

pypi_download: PASS
source_index: https://pypi.org/simple
package_file_host: files.pythonhosted.org
runtime_dependency_count: 19
runtime_dependency_count_windows_applicable: 17
runtime_dependency_count_linux_marker_skipped: 2
transitive_dependency_count: 34
dependency_package_count: 51
wheelhouse_files: 52
dependency_wheel_files: 51
wheelhouse_bytes: 58888431
dependency_wheel_bytes: 57745653
all_binary_wheels: true
sdists: 0
dev_extra_packages: 0
requested_dependency_extras: [crypto, linkify]
manifest_sha256_crosscheck_mismatches: 0

clean_venv_install: PASS
venv_include_system_site_packages: false
install_used_no_index: true
install_exit_code: 0
pip_check: PASS
pip_check_exit_code: 0
pip_list_package_count: 53
installed_version: 6.0.0a3
installed_import: PASS
installed_import_path: C:/Users/ekono/AppData/Local/Temp/karox-mission09-20261106/venv-clean/Lib/site-packages/karox/__init__.py
repository_on_sys_path: false
user_site_enabled_under_isolated: false

cli_version: PASS
cli_paths: PASS
cli_provider_presets: PASS
cli_provider_presets_count: 21
cli_provider_presets_parse_errors: 0
cli_provider_presets_utf8_forced_count: 21
cli_help: PASS
cli_agent_run_help: PASS

quick_gate: PASS
quick_gate_passed: 64
check_tree: PASS
ruff: PASS
mypy: PASS
git_diff_check: PASS

browser_runs: 0
browser_binary_installations: 0
paid_calls: 0
non_pypi_network: 0
global_installations: 0
credentials_accessed: 0
bridge_oauth_mcp_live_calls: 0
source_changes: 0
other_worktrees_changed: 0
push_performed: false
merge_rebase_reset_tag_publish: 0

release_ready: false
verdict:
  - CLEAN_VENV_DEPENDENCY_INSTALL PASS
  - CLEAN_VENV_CLI_SMOKE PASS
  - OFFLINE_RC_CANDIDATE — ADVANCED

residual_blockers:
  - id: live_provider_tui
    status: NOT_RUN
  - id: bridge_oauth_live
    status: NOT_RUN
  - id: external_mcp_live
    status: NOT_RUN
  - id: browser_desktop_3d_live
    status: NOT_RUN
  - id: full_pytest
    status: NOT_RUN
  - id: cross_os_ci
    status: NOT_RUN
  - id: linux_dependency_closure
    status: NOT_RUN
```
