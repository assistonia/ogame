#!/usr/bin/env bash
# Simulation server helper.
#   ./server/sim.sh setup            apply sim overlay to the ogamex submodule
#   ./server/sim.sh up               start db + app + web (runs setup first)
#   ./server/sim.sh down             stop the stack (keeps the DB volume)
#   ./server/sim.sh reset            stop and DELETE the DB volume (fresh universe)
#   ./server/sim.sh artisan ARGS...  run an artisan command in the app container
#   ./server/sim.sh dump FILE        gzip SQL dump of the game DB (checkpoint)
#   ./server/sim.sh restore FILE     load a dump made by `dump`
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OGX="$HERE/ogamex"
COMPOSE=(docker compose --project-directory "$OGX" -f "$OGX/docker-compose.yml" -f "$HERE/docker-compose.sim.yml")

setup() {
  [ -f "$OGX/artisan" ] || git -C "$HERE/.." submodule update --init server/ogamex
  cp -R "$HERE/sim-overlay/." "$OGX/"
  grep -q 'OGame\\Sim\\SimServiceProvider' "$OGX/bootstrap/providers.php" || \
    sed -i 's|^];|    OGame\\Sim\\SimServiceProvider::class,\n];|' "$OGX/bootstrap/providers.php"
  [ -f "$OGX/.env" ] || cp "$OGX/.env.example" "$OGX/.env"
  sed -i 's/^BROADCAST_CONNECTION=.*/BROADCAST_CONNECTION=log/' "$OGX/.env"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  setup) setup ;;
  up)
    setup
    # On a fresh DB volume the app can start before MariaDB accepts connections;
    # its entrypoint then restarts once. Wait for it to turn healthy and retry.
    if ! "${COMPOSE[@]}" up -d --pull missing; then
      for _ in $(seq 1 60); do
        [ "$(docker inspect -f '{{.State.Health.Status}}' "$("${COMPOSE[@]}" ps -q ogamex-app)" 2>/dev/null)" = healthy ] && break
        sleep 5
      done
      "${COMPOSE[@]}" up -d --pull missing
    fi
    echo "waiting for app..."
    for _ in $(seq 1 120); do
      "${COMPOSE[@]}" exec -T ogamex-app php artisan ogamex:sim:clock >/dev/null 2>&1 && break
      sleep 3
    done
    "${COMPOSE[@]}" exec -T ogamex-app php artisan ogamex:sim:clock ;;
  down) "${COMPOSE[@]}" down ;;
  reset) "${COMPOSE[@]}" down -v; rm -f "$OGX/storage/sim/clock" ;;
  artisan) "${COMPOSE[@]}" exec -T -u www-data ogamex-app php artisan "$@" ;;
  dump)
    out="${1:?usage: dump FILE.sql.gz}"
    "${COMPOSE[@]}" exec -T ogamex-db sh -c 'mariadb-dump -uroot -ptoor --single-transaction laravel' | gzip > "$out"
    cp "$OGX/storage/sim/clock" "$out.clock" ;;
  restore)
    in="${1:?usage: restore FILE.sql.gz}"
    gunzip -c "$in" | "${COMPOSE[@]}" exec -T ogamex-db sh -c 'mariadb -uroot -ptoor laravel'
    cp "$in.clock" "$OGX/storage/sim/clock" ;;
  compose) "${COMPOSE[@]}" "$@" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
