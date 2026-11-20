"""Ready-made automations: pick one, and the New automation form opens filled in.

A template is only initial form values (plus a note on what the servers need). Nothing
here runs by itself, and everything stays editable before saving. Scripts run as the agent
user, so they avoid needing root; where something does, the note says which sudo rule.
"""

from dataclasses import dataclass, field

CATEGORIES = {
    "maintenance": ("Maintenance", "Keep packages current and servers tidy."),
    "backups": ("Backups", "Nightly copies with old ones cleaned up."),
    "checks": ("Checks", "Fail loudly when something's wrong, so the run turns red."),
    "deploys": ("Deploys", "Push to a branch and the servers update."),
}


@dataclass(frozen=True)
class Template:
    key: str
    category: str
    title: str
    blurb: str
    kind: str  # action | script | deploy
    initial: dict = field(default_factory=dict)
    needs: str = ""  # what each server must have, shown on the card and the form

    @property
    def when(self) -> str:
        """The trigger, in a few words, for the card."""
        from apps.operations.cron import describe

        if self.initial.get("on_push"):
            return "On push"
        if cron := self.initial.get("cron"):
            return describe(cron).capitalize()
        return "By hand"


def _script(body: str) -> str:
    return body.strip("\n") + "\n"


TEMPLATES = [
    # ------------------------------------------------------------------ maintenance
    Template(
        "weekly-upgrade",
        "maintenance",
        "Weekly package upgrades",
        "Upgrade packages early every Sunday, one server at a time, stopping if one fails.",
        "action",
        {
            "action": "_task:upgrade",
            "cron": "0 3 * * sun",
            "rolling": True,
            "batch_size": 1,
            "pause_seconds": 60,
            "stop_on_failure": True,
        },
        needs="sudo smctl tasks on upgrade",
    ),
    Template(
        "daily-update-check",
        "maintenance",
        "Daily update check",
        "Each morning, list the packages waiting to be upgraded. Changes nothing.",
        "action",
        {"action": "_task:updates", "cron": "0 8 * * *"},
        needs="sudo smctl tasks on updates",
    ),
    Template(
        "weekly-cleanup",
        "maintenance",
        "Weekly cleanup",
        "Remove unused packages, clear the package cache and trim old journal logs.",
        "action",
        {"action": "_task:cleanup", "cron": "30 3 * * sun"},
        needs="sudo smctl tasks on cleanup",
    ),
    Template(
        "rolling-reboot",
        "maintenance",
        "Rolling reboot",
        "Reboot servers one at a time, two minutes apart, after an Admin approves.",
        "action",
        {
            "action": "_task:reboot",
            "require_approval": True,
            "rolling": True,
            "batch_size": 1,
            "pause_seconds": 120,
            "stop_on_failure": True,
        },
        needs="sudo smctl tasks on reboot",
    ),
    Template(
        "restart-service",
        "maintenance",
        "Restart a service",
        "One click to restart a systemd service, like nginx or your app.",
        "action",
        {"action": "restart_service", "arg_name": "nginx"},
        needs="sudo smctl service add nginx --restartable",
    ),
    Template(
        "docker-prune",
        "maintenance",
        "Docker cleanup",
        "Delete stopped containers and images unused for a week, so the disk doesn't fill.",
        "script",
        {
            "cron": "0 4 * * sat",
            "body": _script("""
set -euo pipefail
# Removes stopped containers, unused networks, and images unused for 7 days.
docker system prune --all --force --filter "until=168h"
docker system df
"""),
        },
        needs="sudo smctl shell on; sudo smctl docker enable",
    ),
    # ---------------------------------------------------------------------- backups
    Template(
        "postgres-backup",
        "backups",
        "PostgreSQL backup",
        "Dump a database every night to a local folder and keep 14 days of copies.",
        "script",
        {
            "cron": "0 2 * * *",
            "body": _script("""
set -euo pipefail
# Change these:
DB="app"
DIR="/var/backups/postgres"
KEEP_DAYS=14

mkdir -p "$DIR"
FILE="$DIR/$DB-$(date +%Y%m%d-%H%M).sql.gz"
pg_dump --no-owner "$DB" | gzip > "$FILE"
echo "Saved $FILE ($(du -h "$FILE" | cut -f1))"
find "$DIR" -name "$DB-*.sql.gz" -mtime +"$KEEP_DAYS" -print -delete
"""),
        },
        needs="sudo smctl shell on; the agent user can read the database (~/.pgpass or a peer role) and write the folder",
    ),
    Template(
        "mysql-backup",
        "backups",
        "MySQL / MariaDB backup",
        "Dump a database every night, consistent without locking, and keep 14 days.",
        "script",
        {
            "cron": "15 2 * * *",
            "body": _script("""
set -euo pipefail
# Change these:
DB="app"
DIR="/var/backups/mysql"
KEEP_DAYS=14

mkdir -p "$DIR"
FILE="$DIR/$DB-$(date +%Y%m%d-%H%M).sql.gz"
mysqldump --single-transaction --quick --routines "$DB" | gzip > "$FILE"
echo "Saved $FILE ($(du -h "$FILE" | cut -f1))"
find "$DIR" -name "$DB-*.sql.gz" -mtime +"$KEEP_DAYS" -print -delete
"""),
        },
        needs="sudo smctl shell on; credentials in the agent user's ~/.my.cnf; the folder writable",
    ),
    Template(
        "folder-backup",
        "backups",
        "Folder backup",
        "Archive a folder (uploads, configs) every night and keep 7 days of archives.",
        "script",
        {
            "cron": "30 2 * * *",
            "body": _script("""
set -euo pipefail
# Change these:
SRC="/var/www/app/uploads"
DIR="/var/backups/files"
KEEP_DAYS=7

mkdir -p "$DIR"
FILE="$DIR/$(basename "$SRC")-$(date +%Y%m%d-%H%M).tar.gz"
tar -czf "$FILE" -C "$(dirname "$SRC")" "$(basename "$SRC")"
echo "Saved $FILE ($(du -h "$FILE" | cut -f1))"
find "$DIR" -name "$(basename "$SRC")-*.tar.gz" -mtime +"$KEEP_DAYS" -print -delete
"""),
        },
        needs="sudo smctl shell on; the agent user can read the folder and write the backup folder",
    ),
    # ----------------------------------------------------------------------- checks
    Template(
        "disk-check",
        "checks",
        "Disk space check",
        "Every hour, list disk usage and fail if any disk is over 90% full.",
        "script",
        {
            "cron": "0 * * * *",
            "body": _script("""
set -uo pipefail
LIMIT=90  # percent

df -h -x tmpfs -x devtmpfs -x overlay -x squashfs
FULL=$(df --output=pcent,target -x tmpfs -x devtmpfs -x overlay -x squashfs | tail -n +2 \\
  | awk -v limit="$LIMIT" '{ gsub("%", "", $1); if ($1 + 0 >= limit) print $2 " (" $1 "%)" }')
if [ -n "$FULL" ]; then
  echo
  echo "Over $LIMIT%: $FULL"
  exit 1
fi
"""),
        },
        needs="sudo smctl shell on",
    ),
    Template(
        "http-health",
        "checks",
        "Website health check",
        "Every 15 minutes, call your URLs from the server and fail if any doesn't answer 2xx.",
        "script",
        {
            "cron": "*/15 * * * *",
            "body": _script("""
set -uo pipefail
# One URL per line. Internal addresses work: this runs on the server.
URLS="
http://localhost/health
https://example.com/
"

failed=0
for url in $URLS; do
  code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$url" || echo "000")
  echo "$code  $url"
  case "$code" in 2*) ;; *) failed=1 ;; esac
done
exit $failed
"""),
        },
        needs="sudo smctl shell on; curl installed",
    ),
    Template(
        "tls-expiry",
        "checks",
        "Certificate expiry check",
        "Daily, fail if a site's TLS certificate expires within 14 days.",
        "script",
        {
            "cron": "0 9 * * *",
            "body": _script("""
set -uo pipefail
# Change these:
DOMAINS="example.com www.example.com"
WARN_DAYS=14

failed=0
for d in $DOMAINS; do
  end=$(echo | openssl s_client -servername "$d" -connect "$d:443" 2>/dev/null \\
    | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  if [ -z "$end" ]; then echo "??  $d: couldn't read the certificate"; failed=1; continue; fi
  days=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
  echo "$days days  $d"
  [ "$days" -lt "$WARN_DAYS" ] && failed=1
done
exit $failed
"""),
        },
        needs="sudo smctl shell on; openssl installed",
    ),
    Template(
        "certbot-renew",
        "checks",
        "Renew Let's Encrypt certificates",
        "Twice a day, let certbot renew whatever is close to expiring.",
        "script",
        {
            "cron": "17 3,15 * * *",
            "body": _script("""
set -euo pipefail
sudo -n certbot renew --quiet --deploy-hook "systemctl reload nginx"
sudo -n certbot certificates | grep -E "Certificate Name|Expiry"
"""),
        },
        needs="sudo smctl shell on; a sudo rule letting the agent user run certbot",
    ),
    # ---------------------------------------------------------------------- deploys
    Template(
        "deploy-node",
        "deploys",
        "Node.js app",
        "Install with npm ci, build, then restart the app's systemd service.",
        "deploy",
        {
            "branch": "main",
            "app": "web",
            "on_push": True,
            "steps": _script("""
npm ci
npm run build --if-present
sudo -n systemctl restart web   # needs a sudo rule for this exact command
"""),
        },
        needs="One-time setup on each server (the command is shown after saving)",
    ),
    Template(
        "deploy-django",
        "deploys",
        "Django / Python app",
        "Update the virtualenv, migrate, collect static files and restart Gunicorn.",
        "deploy",
        {
            "branch": "main",
            "app": "api",
            "on_push": True,
            "steps": _script("""
python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput
sudo -n systemctl restart api   # needs a sudo rule for this exact command
"""),
        },
        needs="One-time setup on each server (the command is shown after saving)",
    ),
    Template(
        "deploy-compose",
        "deploys",
        "Docker Compose app",
        "Pull and rebuild the stack, start it, and drop the images it no longer uses.",
        "deploy",
        {
            "branch": "main",
            "app": "stack",
            "on_push": True,
            "steps": _script("""
docker compose pull --quiet
docker compose up -d --build --remove-orphans
docker image prune -f
docker compose ps
"""),
        },
        needs="One-time setup on each server (the command is shown after saving); the app user in the docker group",
    ),
    Template(
        "deploy-static",
        "deploys",
        "Static site",
        "Build the site and publish it to the web root in one step.",
        "deploy",
        {
            "branch": "main",
            "app": "site",
            "on_push": True,
            "steps": _script("""
npm ci
npm run build
rsync -a --delete dist/ /var/www/site/   # change dist/ to your build folder
"""),
        },
        needs="One-time setup on each server (the command is shown after saving); the app user owns /var/www/site",
    ),
    Template(
        "deploy-laravel",
        "deploys",
        "Laravel / PHP app",
        "Composer install, migrate, rebuild the caches and reload PHP-FPM.",
        "deploy",
        {
            "branch": "main",
            "app": "app",
            "on_push": True,
            "steps": _script("""
composer install --no-dev --no-interaction --prefer-dist --optimize-autoloader
php artisan migrate --force
php artisan config:cache
php artisan route:cache
php artisan view:cache
sudo -n systemctl reload php8.3-fpm   # needs a sudo rule; match your PHP version
"""),
        },
        needs="One-time setup on each server (the command is shown after saving)",
    ),
]

BY_KEY = {t.key: t for t in TEMPLATES}


def usable(t: Template, user) -> bool:
    """Could this person save it? Scripts need shell; a server task needs that task."""
    from apps.operations import server_tasks
    from apps.operations.forms import TASK_PREFIX

    if t.kind == "script":
        return user.has_perm("operations.run_shell")
    action = t.initial.get("action", "")
    if action.startswith(TASK_PREFIX):
        return server_tasks.can_run(user, action.removeprefix(TASK_PREFIX))
    return True


def available(user) -> list[Template]:
    return [t for t in TEMPLATES if usable(t, user)]


def grouped(user) -> list[tuple[str, str, str, list[Template]]]:
    mine = available(user)
    return [(key, label, blurb, [t for t in mine if t.category == key])
            for key, (label, blurb) in CATEGORIES.items()
            if any(t.category == key for t in mine)]  # fmt: skip
