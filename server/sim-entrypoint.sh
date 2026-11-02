#!/bin/sh
# Simulation app entrypoint (replaces upstream docker/entrypoint.sh for the sim stack).
# Expensive one-time steps (composer install, rust build) are skipped when
# their output already exists, so restarts are fast.
set -e
cd /var/www
[ -f .env ] || cp .env.example .env
git config --global --add safe.directory /var/www 2>/dev/null || true

if [ ! -f vendor/autoload.php ]; then
  composer install --no-dev --no-interaction --prefer-dist
fi
# Rebuild autoload + package manifest with this PHP (cheap, offline).
composer dump-autoload --no-dev --no-interaction -q

if ! grep -qE '^APP_KEY=.+' .env; then php artisan key:generate --force; fi

mkdir -p storage/sim storage/rust-libs
chown -R www-data:www-data storage bootstrap/cache
su -s /bin/sh -c "php artisan migrate --force" www-data
su -s /bin/sh -c "php artisan ogamex:sim:init" www-data || true

# Master runs as root (so it can log to stderr); workers run as www-data.
printf '[www]\nuser = www-data\ngroup = www-data\n' > /usr/local/etc/php-fpm.d/zzz-sim-user.conf
exec php-fpm -F -R
