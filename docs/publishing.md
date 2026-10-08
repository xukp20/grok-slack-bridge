# Publishing this repository

The repository is created locally first; publishing uses the GitHub CLI with
a device login, so no token is pasted anywhere.

```bash
# 1. Log in once (device flow): prints a one-time code and a URL.
gh auth login --hostname github.com --git-protocol https --web
#    Open https://github.com/login/device, enter the code, approve.
gh auth setup-git          # let git use the gh credential
gh auth status

# 2. Create the remote and push (pick public/private).
cd grok-slack-bridge
gh repo create xukp20/grok-slack-bridge --public --source . --remote origin \
  --description "Talk to your agent from Slack: Socket Mode bridge + reusable skill" --push
```

Updating later: commit, then `git push`. Installs that symlink the skill
(the default `install.sh` layout) pick up changes after `git pull --ff-only`
and `scripts/restart.sh`.

Before pushing, confirm no secrets are tracked:

```bash
git ls-files | xargs grep -nE 'xox[abpr]-[0-9A-Za-z-]{10,}|xapp-[0-9]-[A-Z0-9]{8,}|Bearer [A-Za-z0-9._-]{16,}' || echo clean
```
