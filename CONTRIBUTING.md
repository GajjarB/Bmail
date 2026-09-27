# Contributing

Thank you for helping. Bmail is small enough that a good bug report, a new
template recipe for a recruiting system, or a pack for something people track
by mail makes a real difference.

## Licence of contributions

Bmail is under the MIT licence. By sending a pull request you agree that your
contribution is published under the same licence. Only contribute what you
wrote yourself, or what is already under a licence that allows it.

## Never include real mail

Tests and examples use invented people, companies and addresses only
(`example.com`, `.example`). Do not paste real messages, names, addresses,
account numbers or links from anyone's inbox, including your own; rewrite them
with invented details that keep the same shape. Security problems go through
[SECURITY.md](SECURITY.md), not a public issue.

## Getting started

```bash
git clone https://github.com/GajjarB/Bmail.git && cd Bmail
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[html,dev]"
pytest -q            # the whole suite, a few minutes
ruff check src tests
```

The browser tests need Playwright's Chromium (`python -m playwright install
chromium`); without it they are skipped.

## What makes a change easy to accept

- A test that fails before the change and passes after it.
- Words a person would use, in the interface and in the code's comments.
- Nothing new that sends data anywhere. If a change needs the network, say why
  in the pull request.
- New dependencies only under a permissive licence (MIT, BSD, Apache-2.0 and
  the like). `packaging/notices.py` stops a release build that ships anything
  else.

Adding a pack or a recipe: see [docs/PACKS.md](docs/PACKS.md).
