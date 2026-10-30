<?php

namespace OGame\Sim\Commands;

use Illuminate\Console\Command;
use OGame\Sim\SimClock;

class SimClockCommand extends Command
{
    protected $signature = 'ogamex:sim:clock {--set= : Set the clock to this unix time}';

    protected $description = 'Show or set the virtual simulation clock';

    public function handle(): int
    {
        if ($this->option('set') !== null) {
            SimClock::write((int) $this->option('set'));
        }
        $this->line(json_encode(['clock' => SimClock::read(), 'enabled' => SimClock::enabled()]));
        return self::SUCCESS;
    }
}
