<?php

namespace OGame\Sim;

use Illuminate\Database\Events\ConnectionEstablished;
use Illuminate\Support\Facades\Event;
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
        if (SimClock::enabled()) {
            Event::listen(ConnectionEstablished::class, function (ConnectionEstablished $event): void {
                $ts = SimClock::read();
                if ($ts !== null) {
                    $event->connection->statement('SET timestamp = ' . (int) SimClock::now());
                }
            });
            // Connections opened by earlier providers already exist; pin them too.
            if (SimClock::read() !== null) {
                SimClock::syncDatabase(SimClock::now());
            }
        }
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
