"""The deploy webhook: a Git host (or a script) tells the orchestrator "deploy this now".

The orchestrator is one place that can receive it; Portainer's own webhook keeps working and
is noticed from outside (see ``watch.py``). Coming through here, the deploy is *guarded*: the
guards run before Portainer is asked to redeploy anything.

Each service has its own secret, generated here and shown once. It lives in the database's
``setting`` table (``deploy.webhook.<slug>``), never in ``services.yaml`` and never in an
API response after it was created. GitHub signs the body with the secret (HMAC), GitLab
sends it in a header, and the generic form takes a bearer token, so the raw secret has to be
kept rather than a hash of it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from orchestrator.config import Settings
from orchestrator.db.models import Setting


def _key(slug: str) -> str:
    return f"deploy.webhook.{slug}"


def has_secret(session: Session, slug: str) -> bool:
    row = session.get(Setting, _key(slug))
    return bool(row and (row.value or {}).get("secret"))


def rotate_secret(session: Session, slug: str) -> str:
    """Make a new secret (replacing any old one) and return it. This is the only time it
    is ever shown."""
    secret = secrets.token_urlsafe(32)
    row = session.get(Setting, _key(slug)) or Setting(key=_key(slug))
    row.value = {"secret": secret}
    row.updated_at = datetime.now(UTC)
    session.add(row)
    session.commit()
    return secret


def remove_secret(session: Session, slug: str) -> bool:
    row = session.get(Setting, _key(slug))
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _secret(session: Session, slug: str) -> str:
    row = session.get(Setting, _key(slug))
    return str((row.value or {}).get("secret", "")) if row else ""


def webhook_url(settings: Settings, slug: str) -> str:
    base = (settings.external_url or settings.orchestrator_public_url).strip().rstrip("/")
    return f"{base}/hooks/deploy/{slug}" if base else f"/hooks/deploy/{slug}"


def setup_hint(provider: str, url: str) -> list[str]:
    """What to enter on the other side. The secret itself is never part of it."""
    if provider == "gitlab":
        return [
            "GitLab > Settings > Webhooks > Add new webhook",
            f"URL: {url}",
            "Secret token: the secret shown when the webhook was created",
            "Trigger: Push events (pick the branch)",
        ]
    if provider == "github":
        return [
            "GitHub > Settings > Webhooks > Add webhook",
            f"Payload URL: {url}",
            "Content type: application/json",
            "Secret: the secret shown when the webhook was created",
            "Events: Just the push event",
        ]
    return [
        f"POST {url}",
        "Header: Authorization: Bearer <the secret shown when the webhook was created>",
        'Optional JSON body: {"ref": "refs/heads/dev", "sha": "..."} (checked against the branch)',
    ]


# -- verifying a request -------------------------------------------------------------------------


class WebhookRejected(Exception):
    """Not authenticated (or not a webhook at all)."""


@dataclass(frozen=True)
class Delivery:
    """A verified delivery, reduced to what the pipeline records."""

    provider: str
    event: str
    ref: str = ""
    sha: str = ""
    pusher: str = ""
    #: a push or a plain trigger that should start a deploy (False = acknowledge only)
    deploys: bool = True
    note: str = ""

    def as_detail(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            # not "event": the step recorder logs these as keyword arguments
            "hook_event": self.event,
            "ref": self.ref,
            "commit": self.sha[:12],
            "pusher": self.pusher,
        }


def _equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _bearer(headers: dict[str, str], query: dict[str, str]) -> str:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("x-deploy-token", "") or query.get("token", "")


def verify(
    session: Session,
    slug: str,
    provider: str,
    *,
    headers: dict[str, str],
    query: dict[str, str],
    body: bytes,
    payload: dict[str, Any],
) -> Delivery:
    """Check the request against the service's secret and parse it. ``headers`` keys are
    lower-case. Raises :class:`WebhookRejected` when it does not authenticate."""
    secret = _secret(session, slug)
    if not secret:
        raise WebhookRejected("no secret has been created for this webhook")

    # The service's key as a bearer token works whatever the provider is (it is the same key):
    # that is how a GitHub Actions job - or any script - calls it. GitLab's header and GitHub's
    # signature below are the providers' own ways of presenting it.
    presented = _bearer(headers, query)
    if presented:
        if not _equal(presented, secret):
            raise WebhookRejected("bad token")
        return Delivery(
            "github-actions" if headers.get("x-deploy-source") == "github-actions" else "generic",
            "trigger",
            ref=str(payload.get("ref", "")),
            sha=str(payload.get("sha") or payload.get("commit") or ""),
            pusher=str(payload.get("pusher") or payload.get("user") or ""),
        )

    if provider == "gitlab":
        if not _equal(headers.get("x-gitlab-token", ""), secret):
            raise WebhookRejected("bad token")
        event = headers.get("x-gitlab-event", "")
        if event and event != "Push Hook":
            return Delivery("gitlab", event, deploys=False, note=f"{event} ignored")
        return Delivery(
            "gitlab",
            event or "Push Hook",
            ref=str(payload.get("ref", "")),
            sha=str(payload.get("checkout_sha") or payload.get("after") or ""),
            pusher=str(payload.get("user_username") or payload.get("user_name") or ""),
        )

    if provider == "github":
        theirs = headers.get("x-hub-signature-256", "")
        ours = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not _equal(theirs, ours):
            raise WebhookRejected("bad signature")
        event = headers.get("x-github-event", "")
        if event == "ping":
            return Delivery("github", "ping", deploys=False, note="pong")
        if event and event != "push":
            return Delivery("github", event, deploys=False, note=f"{event} ignored")
        return Delivery(
            "github",
            event or "push",
            ref=str(payload.get("ref", "")),
            sha=str(payload.get("after") or ""),
            pusher=str((payload.get("pusher") or {}).get("name", "")),
        )

    if not _equal(_bearer(headers, query), secret):
        raise WebhookRejected("bad token")
    return Delivery(
        "generic",
        "trigger",
        ref=str(payload.get("ref", "")),
        sha=str(payload.get("sha") or payload.get("commit") or ""),
        pusher=str(payload.get("pusher") or payload.get("user") or ""),
    )


def authenticate(
    session: Session, slug: str, *, headers: dict[str, str], query: dict[str, str]
) -> None:
    """Accept a request that carries this service's key (bearer, ``X-Deploy-Token``,
    ``?token=`` or GitLab's header). Raises :class:`WebhookRejected`."""
    secret = _secret(session, slug)
    if not secret:
        raise WebhookRejected("no secret has been created for this webhook")
    presented = _bearer(headers, query) or headers.get("x-gitlab-token", "")
    if not _equal(presented, secret):
        raise WebhookRejected("bad token")


def secret_name(slug: str) -> str:
    """The name of the repository secret that holds this service's key: one per service."""
    return "DEPLOY_KEY_" + re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_").upper()


_WORKFLOW = """\
# .github/workflows/deploy-@SLUG@.yml
# Deploys @SLUG@ through the orchestrator's guards (probes, backup) whenever @BRANCH@ is pushed,
# and fails this job - with the reason - if the orchestrator cannot be reached, refuses the key,
# blocks the deploy, or the deploy does not end up healthy.
#
# One-time setup: add a repository secret named @SECRET@ holding this service's key
# (GitHub: Settings > Secrets and variables > Actions > New repository secret).
# The runner must be able to reach @URL@ - a GitHub-hosted runner needs it
# reachable from the internet; otherwise use a self-hosted runner on your network.
name: Deploy @SLUG@
on:
  push:
    branches: [@BRANCH@]
  workflow_dispatch:
concurrency:
  group: deploy-@SLUG@
  cancel-in-progress: false
jobs:
  deploy:
    runs-on: ubuntu-latest
    timeout-minutes: 40
    steps:
      - name: Deploy @SLUG@ through the orchestrator
        env:
          DEPLOY_URL: @URL@
          DEPLOY_KEY: @KEY@
          REF: ${{ github.ref }}
          SHA: ${{ github.sha }}
          ACTOR: ${{ github.actor }}
        run: |
          set -u
          fail() { echo "::error title=Deploy of @SLUG@ failed::$1"; exit 1; }

          [ -n "${DEPLOY_KEY:-}" ] || fail "The key is empty: add the repository secret @SECRET@."
          body=$(jq -nc --arg ref "$REF" --arg sha "$SHA" --arg pusher "$ACTOR" \
            '{ref: $ref, sha: $sha, pusher: $pusher}')

          # 1. ask the orchestrator to deploy
          code=$(curl -sS -o resp.json -w '%{http_code}' --max-time 30 -X POST "$DEPLOY_URL" \
            -H "Authorization: Bearer $DEPLOY_KEY" -H 'X-Deploy-Source: github-actions' \
            -H 'Content-Type: application/json' -d "$body") \
            || fail "Could not reach the orchestrator at $DEPLOY_URL (down, wrong URL, or not reachable from this runner)."
          why=$(jq -r '.detail // empty' resp.json 2>/dev/null || true)
          case "$code" in
            202) ;;
            401) fail "The orchestrator rejected the key (401). The secret @SECRET@ must be this service's current key - it changes when the key is replaced." ;;
            404) fail "The orchestrator has no deploy webhook for @SLUG@ (404): switch the webhook on for this service." ;;
            409) fail "The orchestrator refused to deploy (409): ${why:-busy or deploys switched off}" ;;
            423) fail "@SLUG@ is switched off in the orchestrator (423): switch it on first." ;;
            5??) fail "The orchestrator failed ($code): ${why:-$(head -c 300 resp.json)}" ;;
            *)   fail "Unexpected answer $code from the orchestrator: $(head -c 300 resp.json)" ;;
          esac
          if [ "$(jq -r '.started' resp.json)" != "true" ]; then
            echo "Nothing to deploy: $(jq -r '.ignored // "acknowledged"' resp.json)"
            exit 0
          fi
          run=$(jq -r '.run_id' resp.json)
          echo "Deploy run #$run started."

          # 2. wait for the result (the guards and the health checks take a while)
          bad=0; waited=0
          while [ "$waited" -lt 1800 ]; do
            sleep "${DEPLOY_POLL_S:-10}"; waited=$((waited + ${DEPLOY_POLL_S:-10}))
            if ! code=$(curl -sS -o run.json -w '%{http_code}' --max-time 20 \
                 -H "Authorization: Bearer $DEPLOY_KEY" "$DEPLOY_URL/runs/$run"); then
              code=000
            fi
            if [ "$code" != 200 ]; then
              bad=$((bad + 1))
              [ "$bad" -lt 6 ] && { echo "status check answered $code ($bad/6), retrying"; continue; }
              fail "Lost contact with the orchestrator while the deploy ran (last answer $code). Check run #$run there."
            fi
            bad=0
            case "$(jq -r '.status' run.json)" in
              running) echo "  still running: $(jq -r '[.steps[].name] | last // "starting"' run.json)" ;;
              succeeded) echo "Deployed and healthy."; exit 0 ;;
              skipped) fail "Blocked before anything changed: $(jq -r '.error // "a guard said no"' run.json)" ;;
              *) fail "The deploy failed: $(jq -r '.error // "see run #'"$run"' in the orchestrator"' run.json). Steps: $(jq -r '[.steps[] | select(.status == "failed") | .name + (if .message != "" then " (" + .message + ")" else "" end)] | join("; ")' run.json)" ;;
            esac
          done
          fail "Gave up waiting after 30 minutes. The deploy (run #$run) may still be running in the orchestrator."
"""


def github_workflow(slug: str, url: str, branch: str, *, key: str | None = None) -> dict[str, str]:
    """The GitHub Actions workflow that triggers this service's webhook, with its error handling.
    The key is the repository secret's name unless ``key`` (the real key, shown only right after it
    is created) is given."""
    name = secret_name(slug)
    text = (
        _WORKFLOW.replace("@SLUG@", slug)
        .replace("@BRANCH@", branch or "main")
        .replace("@SECRET@", name)
        .replace("@URL@", url)
        .replace("@KEY@", key if key else "${{ secrets." + name + " }}")
    )
    return {"secret_name": name, "workflow": text, "path": f".github/workflows/deploy-{slug}.yml"}


def branch_matches(want: str, ref: str) -> bool:
    """No filter, or the push is to that branch. A delivery that names no ref (the generic
    form) is accepted: the sender chose to trigger it."""
    want = want.strip()
    if not want or not ref:
        return True
    return ref in {want, f"refs/heads/{want}"}
