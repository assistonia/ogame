<?php

namespace OGame\Sim;

use Illuminate\Support\ServiceProvider;

class SimServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        if (SimClock::enabled()) {
            require_once __DIR__ . '/time_overrides.php';
            SimClock::apply();
        }
    }

    public function boot(): void
    {
        if ($this->app->runningInConsole()) {
            $this->commands([
                Commands\SimInit::class,
                Commands\SimClockCommand::class,
                Commands\SimSeedBots::class,
                Commands\SimAdvance::class,
                Commands\SimScores::class,
            ]);
        }
    }
}
