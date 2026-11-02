<?php

namespace OGame\Sim\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\Hash;
use Illuminate\Support\Str;
use OGame\Models\User;
use OGame\Services\SettingsService;
use OGame\Sim\SimClock;

/**
 * One-time / idempotent sim setup: start the virtual clock and apply
 * universe settings. Speeds only change when passed explicitly.
 */
class SimInit extends Command
{
    protected $signature = 'ogamex:sim:init
        {--speed= : Set economy, research and fleet speeds to this value}
        {--clock= : Set the virtual clock (unix seconds). Default: keep, or real now if unset}
        {--battle-engine=php : php or rust}';

    protected $description = 'Initialise the simulation clock and universe settings';

    public function handle(SettingsService $settings): int
    {
        $clock = $this->option('clock');
        if ($clock !== null) {
            SimClock::write((int) $clock);
        } elseif (SimClock::read() === null) {
            SimClock::write(\time());
        }

        // OGameX makes the first registered non-Legor user an admin. Claim that
        // slot with a dedicated account so no bot gets admin powers.
        if (!User::where('email', 'simadmin@bots.local')->exists()
            && User::where('username', '!=', 'Legor')->count() === 0) {
            User::create([
                'lang' => 'en',
                'username' => 'simadmin',
                'email' => 'simadmin@bots.local',
                'password' => Hash::make(Str::random(32)),
            ]);
        }

        $engine = (string) $this->option('battle-engine');
        if (in_array($engine, ['php', 'rust'], true)) {
            $settings->set('battle_engine', $engine);
        }

        $speed = $this->option('speed');
        if ($speed !== null) {
            foreach (['economy_speed', 'research_speed', 'fleet_speed', 'fleet_speed_war', 'fleet_speed_holding', 'fleet_speed_peaceful'] as $key) {
                $settings->set($key, (int) $speed);
            }
        }

        $this->line(json_encode([
            'clock' => SimClock::read(),
            'clock_enabled' => SimClock::enabled(),
            'battle_engine' => $settings->get('battle_engine', 'rust'),
            'economy_speed' => $settings->economySpeed(),
            'research_speed' => $settings->researchSpeed(),
            'fleet_speed' => $settings->fleetSpeed(),
        ]));

        return self::SUCCESS;
    }
}
