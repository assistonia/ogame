<?php

namespace OGame\Sim\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\Hash;
use OGame\Enums\CharacterClass;
use OGame\Factories\PlayerServiceFactory;
use OGame\Models\Resources;
use OGame\Models\User;
use OGame\Services\InitialUserDataService;

/**
 * Create bot accounts the same way normal registration does (user + tech row +
 * homeworld). Idempotent: existing emails are skipped.
 *
 * Example: ogamex:sim:seed-bots --prefix=bot --count=30 --class=collector
 * Inactive farm targets: --prefix=farm --count=8 --inactive-days=10 --mines=12
 */
class SimSeedBots extends Command
{
    protected $signature = 'ogamex:sim:seed-bots
        {--prefix=bot : Account name prefix}
        {--count=10 : Number of accounts}
        {--start=1 : First account number}
        {--pad=3 : Zero padding of the number}
        {--domain=bots.local : Email domain}
        {--password=botpass : Password for all accounts}
        {--class= : collector, general, discoverer or empty for none}
        {--inactive-days=0 : Mark accounts as inactive this many days ago}
        {--mines=0 : Preset metal/crystal/deut mine level (for farm targets)}
        {--resources=0 : Preset resources of each kind on the homeworld}';

    protected $description = 'Create simulation bot accounts';

    public function handle(InitialUserDataService $initial, PlayerServiceFactory $players): int
    {
        $prefix = (string) $this->option('prefix');
        $count = (int) $this->option('count');
        $start = (int) $this->option('start');
        $pad = (int) $this->option('pad');
        $domain = (string) $this->option('domain');
        $password = Hash::make((string) $this->option('password'));
        $class = $this->parseClass((string) $this->option('class'));
        $inactiveDays = (int) $this->option('inactive-days');
        $mines = (int) $this->option('mines');
        $resources = (int) $this->option('resources');

        $created = [];
        $skipped = [];
        for ($i = $start; $i < $start + $count; $i++) {
            $name = $prefix . str_pad((string) $i, $pad, '0', STR_PAD_LEFT);
            $email = $name . '@' . $domain;
            if (User::where('email', $email)->exists()) {
                $skipped[] = $email;
                continue;
            }

            $user = User::create([
                'lang' => 'en',
                'username' => $name,
                'email' => $email,
                'password' => $password,
            ]);
            $user->first_login = false;
            if ($class !== null) {
                $user->character_class = $class->value;
                $user->character_class_free_used = true;
                $user->character_class_changed_at = now();
            }
            $user->time = (string) now()->subDays($inactiveDays)->timestamp;
            $user->save();

            $initial->createFor($user);

            if ($mines > 0 || $resources > 0) {
                $player = $players->make($user->id, true);
                $planet = $player->planets->first();
                if ($planet !== null) {
                    if ($mines > 0) {
                        foreach (['metal_mine', 'crystal_mine', 'deuterium_synthesizer', 'solar_plant'] as $building) {
                            $planet->setObjectLevel($this->objectId($building), $mines, false);
                        }
                        // Storage big enough that the farm keeps producing loot.
                        foreach (['metal_store', 'crystal_store', 'deuterium_store'] as $building) {
                            $planet->setObjectLevel($this->objectId($building), max(1, intdiv($mines, 2)), false);
                        }
                    }
                    if ($resources > 0) {
                        $planet->addResources(new Resources($resources, $resources, $resources), false);
                    }
                    $planet->save();
                }
            }

            // Compute production stats now so the first clock advance already produces.
            foreach ($players->make($user->id, true)->planets->all() as $planet) {
                $planet->update();
            }

            // Keep the inactive timestamp: createFor() may touch the user row.
            if ($inactiveDays > 0) {
                $user->refresh();
                $user->time = (string) now()->subDays($inactiveDays)->timestamp;
                $user->save();
            }
            $created[] = $email;
        }

        $this->line(json_encode(['created' => $created, 'skipped' => $skipped]));
        return self::SUCCESS;
    }

    private function parseClass(string $value): CharacterClass|null
    {
        return match (strtolower(trim($value))) {
            'collector', 'miner' => CharacterClass::COLLECTOR,
            'general', 'warrior' => CharacterClass::GENERAL,
            'discoverer', 'explorer' => CharacterClass::DISCOVERER,
            default => null,
        };
    }

    private function objectId(string $machineName): int
    {
        return \OGame\Services\ObjectService::getObjectByMachineName($machineName)->id;
    }
}
