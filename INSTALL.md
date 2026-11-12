# Install af

af provides the `af` command and one host skill. Matt Pocock's skills and the worker CLIs are installed separately.

## Link the skill and install the command

For Claude Code:

```sh
mkdir -p ~/.claude/skills
ln -s /path/to/af/skills/af ~/.claude/skills/af
```

Link `skills/af` into the skill directory for another Agent Skills host. To put `af` on `PATH`, either add the `bin` directory:

```sh
export PATH="/path/to/af/bin:$PATH"
af --help
```

Or install the command as a symlink:

```sh
mkdir -p ~/.local/bin
ln -s /path/to/af/bin/af ~/.local/bin/af
export PATH="$HOME/.local/bin:$PATH"
af --help
```

## Install Matt Pocock's skills

Follow the upstream [mattpocock/skills](https://github.com/mattpocock/skills) installation instructions. `af doctor` checks for `grill-with-docs`, `to-spec`, `to-tickets`, `implement-spec`, `implement`, `tdd`, `code-review`, `diagnosing-bugs`, `pr`, and `retro`. Set `skill_dirs` in the config when they are outside the default locations. Each folder must contain `SKILL.md`.

Run `/setup-matt-pocock-skills` once in each target repository. `af doctor` warns when `docs/agents/issue-tracker.md` or `docs/agents/domain.md` is missing.

## Configure and check

Copy the example to the primary checkout root and configure worker candidates, test commands, risk paths, and retention:

```sh
cp /path/to/af/af.config.example.json ./af.config.json
af doctor --repo .
```

Config resolution is `--config`, `$AF_CONFIG`, the primary checkout's `af.config.json`, then `~/.config/af/af.config.json`. A file inside an `af/<flow-id>` worktree is never selected. `af state start` stores the resolved config path in the flow record.

See [skills/af/SKILL.md](skills/af/SKILL.md) for the flow procedure and [README.md](README.md) for supported config fields and safety behavior. Run `bash tests/run.sh` for offline verification.
