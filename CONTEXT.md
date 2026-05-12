# Local generation and publishing (GitHub Pages)

This document describes how to run the digest **on your machine**, then **commit and push** so the static site under `docs/` is served at your GitHub Pages URL (for example `https://<user>.github.io/<repo>/`).

GitHub Actions can still run on a schedule; if you switch to **local-only** generation, consider disabling the scheduled workflow (or use only manual dispatch) so two sources do not overwrite each other’s `docs/` commits.

---

## Prerequisites

- Python 3.9+ recommended (same major version as CI if possible).
- A **MiniMax API key** available in the environment as `MINIMAX_API_KEY` (for example `export` in `~/.zshrc`). **Do not** commit the key or a `.env` file that contains it.
- Git remote configured (`origin`) with push access to the branch GitHub Pages uses (usually `main`).

---

## One-time setup

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The virtualenv directory `.venv/` is listed in `.gitignore` and must not be committed.

---

## Generate HTML and Markdown locally

Activate the venv and ensure the API key is loaded (open a new terminal after editing `~/.zshrc`, or `source ~/.zshrc`).

```bash
cd /path/to/karpathy-rss-digest
source .venv/bin/activate
```

Typical runs:

```bash
# Last calendar day of posts (default filter: tech / AI / business)
python rss_reader.py --days 1

# Wider window
python rss_reader.py --days 3

# Reproduce a specific lower time bound (ISO 8601, UTC), e.g. from a CI log line “自 … UTC”
python rss_reader.py --since "2026-05-10T16:33:00Z"

# Include all categories (no LLM-based category filter)
python rss_reader.py --days 1 --no-filter
```

**Outputs**

- **Site HTML:** `docs/<today>.html` and updated `docs/index.html` (today is the machine’s local date when the script runs).
- **Markdown copy:** `output/digest-<today>.md` — the `output/` directory is gitignored; only `docs/` is needed for Pages.

Optional: unset `WECOM_WEBHOOK_URL` for a dry run if you do not want WeCom side effects or the sent-article database under `output/`.

---

## Publish: commit and push

After a successful run, publish by pushing the generated **docs** tree to GitHub:

```bash
git status
git add docs/
git commit -m "docs: update digest for $(date +%Y-%m-%d)"
git push origin main
```

Adjust the branch name if your default branch or Pages source branch is not `main`.

GitHub Pages (when configured to serve from `/docs` on that branch) will pick up the new commit; the public URL may take a short time to refresh.

---

## Quick reference

| Step | Command / note |
|------|------------------|
| Install deps | `pip install -r requirements.txt` inside `.venv` |
| API key | `MINIMAX_API_KEY` in environment (e.g. `~/.zshrc`) |
| Generate | `python rss_reader.py --days 1` |
| Publish | `git add docs/ && git commit && git push` |

---

## Troubleshooting

- **`MINIMAX_API_KEY` not set:** Use a login shell or `source ~/.zshrc` before running, or export the variable in the current session only.
- **PyPI timeouts when installing:** `pip install -r requirements.txt --default-timeout=300 --resume-retries 20`
- **Empty digest after filtering:** Try `--no-filter` once to confirm feeds work; otherwise check logs for `筛选后无相关文章`.
