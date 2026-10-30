<?php

namespace OGame\Sim;

use Carbon\Carbon;
use Carbon\CarbonImmutable;
use Illuminate\Support\Facades\Date;

/**
 * Virtual game clock for turn-based simulation.
 *
 * The current simulated unix time is stored in storage/sim/clock. When the
 * clock is enabled, every request and artisan command freezes "now" at that
 * value (Carbon test-now), so the game only moves forward when the simulator
 * calls `ogamex:sim:advance`.
 */
class SimClock
{
    public static function enabled(): bool
    {
        $flag = strtolower((string) env('SIM_CLOCK_ENABLED', '0'));
        return in_array($flag, ['1', 'true', 'yes', 'on'], true);
    }

    public static function path(): string
    {
        return storage_path('sim/clock');
    }

    public static function read(): int|null
    {
        $path = self::path();
        if (!is_file($path)) {
            return null;
        }
        $raw = trim((string) @file_get_contents($path));
        return ctype_digit($raw) ? (int) $raw : null;
    }

    public static function write(int $timestamp): void
    {
        $dir = dirname(self::path());
        if (!is_dir($dir)) {
            mkdir($dir, 0775, true);
        }
        $tmp = self::path() . '.tmp' . getmypid();
        file_put_contents($tmp, (string) $timestamp);
        rename($tmp, self::path());
        self::freeze($timestamp);
    }

    public static function freeze(int $timestamp): void
    {
        $now = Carbon::createFromTimestamp($timestamp, 'UTC');
        Carbon::setTestNow($now);
        CarbonImmutable::setTestNow($now->toImmutable());
        Date::setTestNow($now);
    }

    /** Freeze "now" at the stored clock value if the clock is enabled and set. */
    public static function apply(): void
    {
        if (!self::enabled()) {
            return;
        }
        $ts = self::read();
        if ($ts !== null) {
            self::freeze($ts);
        }
    }

    public static function now(): int
    {
        return Date::now()->timestamp;
    }
}
