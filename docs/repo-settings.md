# Repository settings

What to set on GitHub once the public repository is created from `scripts/publish-snapshot.sh`. None of it lives in the tree. Run the steps in order after the first push, from the snapshot's directory, with the GitHub CLI signed in as a repository admin (`gh auth status`). Where GitHub has no API, the step names the web setting.

```sh
R=epheterson/car-command
```

## 1. About: description, website, topics

```sh
gh repo edit "$R" \
  --description "Drive and charge history for electric cars, on your own server." \
  --homepage "" \
  --add-topic self-hosted,electric-vehicle,ev,tesla,lucid,carconnectivity,trip-logger,charging,battery-health,postgresql,fastapi,docker,python
```

The website stays empty; the README is the front page. In the About panel (the gear), keep **Packages** and **Releases** ticked and untick **Deployments**.

## 2. Features and merging

```sh
gh repo edit "$R" \
  --enable-issues --enable-discussions --enable-wiki=false --enable-projects=false \
  --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false \
  --delete-branch-on-merge --allow-update-branch
```

## 3. Social preview

No API. **Settings › General › Social preview › Edit › Upload an image**: `brand/social-preview.png` (1280 × 640), rebuilt by `brand/make-demo-media.sh`.

## 4. Discussions categories

No API. In **Discussions › Categories** (the pencil):

| Category | Format | Who can post | What for |
|---|---|---|---|
| Announcements | Announcement | maintainers | releases and anything every install should know |
| Q&A | Question and answer | anyone | setup, which source to use, what a figure means (the issue chooser sends questions here) |
| Ideas | Open-ended | anyone | features before they are issues (the roadmap links here) |
| Show and tell | Open-ended | anyone | screenshots of your garage, a make that works through CarConnectivity |
| Sources | Open-ended | anyone | one thread per make and source: what works, what does not |

Delete **General** and **Polls**. Keep the slugs `q-a`, `ideas` and `show-and-tell`: `.github/ISSUE_TEMPLATE/config.yml` and `docs/roadmap.md` link to them. Pin a welcome post in Announcements that points at the README's quick start and `make demo`.

## 5. Code security

```sh
# Private vulnerability reporting (SECURITY.md sends reports there)
gh api -X PUT "repos/$R/private-vulnerability-reporting"

# Dependabot alerts, then Dependabot security updates
gh api -X PUT "repos/$R/vulnerability-alerts"
gh api -X PUT "repos/$R/automated-security-fixes"

# Secret scanning and push protection
gh api -X PATCH "repos/$R" --input - <<'EOF'
{"security_and_analysis": {
  "secret_scanning": {"status": "enabled"},
  "secret_scanning_push_protection": {"status": "enabled"}
}}
EOF
```

Code scanning needs no setting: `.github/workflows/codeql.yml` and `scorecard.yml` upload to it, and it turns on with the first upload. Leave **Settings › Code security › Code scanning › CodeQL analysis** on "advanced" (the workflow); do not also switch on default setup, or the two collide.

Version updates come from `.github/dependabot.yml` (pip for the site and the connector, the Dockerfiles, the compose images, the actions), weekly and grouped. It holds back Postgres majors and Python minors, which need a person.

## 6. Actions

```sh
# The default token starts read-only; each workflow asks for what it writes, job by job.
gh api -X PUT "repos/$R/actions/permissions/workflow" \
  -F default_workflow_permissions=read -F can_approve_pull_request_reviews=false

# Actions on, any action allowed (every one in the tree is pinned to a commit).
gh api -X PUT "repos/$R/actions/permissions" -F enabled=true -f allowed_actions=all

# First-time contributors' pull requests wait for a maintainer to approve the run.
gh api -X PUT "repos/$R/actions/permissions/fork-pr-contributor-approval" -f approval_policy=first_time_contributors
```

Optional secret: `EIA_API_KEY`, a key from eia.gov/opendata, for the monthly rate-averages workflow (it publishes to the `rates-data` branch). Without it the workflow uses EIA's rate-limited demo key.

```sh
gh secret set EIA_API_KEY --repo "$R"
```

No other secret is needed. The release image signs in with the workflow's own `GITHUB_TOKEN`, and signs and attests with its OIDC token.

## 7. Rules for main

One ruleset: changes reach `main` through a pull request whose checks pass, history stays linear, and nobody force-pushes or deletes it. The owner can bypass it, as the only maintainer.

```sh
gh api -X POST "repos/$R/rulesets" --input - <<'EOF'
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "required_linear_history"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": true,
      "allowed_merge_methods": ["squash"]
    }},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": false,
      "do_not_enforce_on_create": true,
      "required_status_checks": [
        {"context": "pytest", "integration_id": 15368},
        {"context": "format", "integration_id": 15368},
        {"context": "the local maps add-on", "integration_id": 15368},
        {"context": "a fresh install", "integration_id": 15368},
        {"context": "an existing install takes this build", "integration_id": 15368},
        {"context": "one container, a fresh install", "integration_id": 15368},
        {"context": "one container, an existing install takes this build", "integration_id": 15368},
        {"context": "one container builds for arm64", "integration_id": 15368},
        {"context": "a compose install moves into one container", "integration_id": 15368},
        {"context": "analyze (python)", "integration_id": 15368},
        {"context": "analyze (javascript-typescript)", "integration_id": 15368},
        {"context": "analyze (actions)", "integration_id": 15368}
      ]
    }},
    {"type": "code_scanning", "parameters": {"code_scanning_tools": [
      {"tool": "CodeQL", "security_alerts_threshold": "high_or_higher", "alerts_threshold": "errors"}
    ]}}
  ]
}
EOF
```

`actor_id 5` is the repository admin role; `integration_id 15368` is GitHub Actions, so only a workflow can satisfy a check. The check names are the job names in `test.yml`, `install.yml` and `codeql.yml`; rename a job and the rule waits forever for the old name, so change both together. The install path runs on every pull request for that reason, whatever it touches.

Check it: `gh api "repos/$R/rulesets" --jq '.[].name'`, then open a pull request and see the checks listed as required.

## 8. Labels

`.github/labels.yml` lists them; `.github/workflows/labels.yml` makes the repository match on every change to that file on main. For the first time, before any push has run it:

```sh
gh workflow run labels.yml --repo "$R"
```

## 9. Packages (the one-container image)

`.github/workflows/release.yml` runs when `stack/web/version.py` changes on main: it tags the commit `v<version>`, calls `release-image.yml` to build `stack/aio/Dockerfile` for amd64 and arm64 and push it to `ghcr.io/epheterson/car-command:<version>`, and drafts the release from the version's `CHANGELOG.md` section. Publishing the draft runs `publish.yml`, which points `:<major>.<minor>` and `:latest` at that image; a pre-release moves neither. Each image carries an SBOM and build provenance in its index, a GitHub build-provenance attestation, and a keyless cosign signature, all from the run's own identity; the image workflow's header says how to verify each. A private copy of the repository never tags or publishes.

After the first release run:

- The package starts **private**. Open it (the repository page's **Packages**), **Package settings › Change visibility › Public**. Until then the README's `docker run` line asks for a login. There is no API for this.
- In the same settings, check that it is linked to this repository (the image's `org.opencontainers.image.source` label does that) and that this repository has **Admin** under **Manage Actions access**, so later tags can push.
- Pull it on a clean machine with the README's line, and verify it: `gh attestation verify oci://ghcr.io/epheterson/car-command:0.1.0 --repo epheterson/car-command`.

## 10. The first release

- Pushing the snapshot's main (version `0.1.0`) runs `release.yml`: tag, image, and a draft release. If it did not start (a repository made public after the push, or Actions turned on later), run it from Actions. Make the package public (above), check the image, then publish the draft.

- Pin the roadmap: open an issue titled "Roadmap" with `docs/roadmap.md` as its body, then pin it.

  ```sh
  url=$(gh issue create --repo "$R" --title "Roadmap" --body-file docs/roadmap.md --label enhancement)
  gh issue pin "$url"
  ```

- The Scorecard badge in the README shows a score from the first run of `scorecard.yml` on main (it publishes only from the default branch of a public repository). Run it by hand once the settings above are in, so the first score counts them: `gh workflow run scorecard.yml --repo "$R"`.
- Then, in the README, the star-history block can be uncommented once there are stars worth charting.
