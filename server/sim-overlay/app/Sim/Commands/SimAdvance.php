<?php

namespace OGame\Sim\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\Artisan;
use OGame\Factories\PlayerServiceFactory;
use OGame\Models\FleetMission;
use OGame\Models\Planet;
use OGame\Models\User;
use OGame\Sim\SimClock;
use Throwable;

/**
 * Move the virtual clock forward and settle the universe.
 *
 * OGameX updates lazily (a player is only updated when they load a page).
 * Bots that do not act, and fleets that land between two bot turns, would
 * otherwise wait. This command walks the clock through every fleet arrival in
 * the window in time order, processes those missions, then updates every
 * player's research queue and every planet (resources, build/unit queues).
 */
class SimAdvance extends Command
{
    protected $signature = 'ogamex:sim:advance
        {--seconds=600 : How far to move the clock}
        {--scores : Regenerate highscores afterwards}';

    protected $description = 'Advance the virtual clock and process all due game events';

    public function handle(PlayerServiceFactory $players): int
    {
        $start = SimClock::read();
        if ($start === null) {
            $this->error('Clock not initialised; run ogamex:sim:init first.');
            return self::FAILURE;
        }
        $target = $start + max(0, (int) $this->option('seconds'));
        $errors = [];
        $missionsProcessed = 0;
        $steps = 0;

        // 1) Walk through fleet events inside the window in chronological order.
        $cursor = $start;
        while (true) {
            $next = $this->nextMissionEvent($cursor, $target);
            if ($next === null) {
                break;
            }
            SimClock::write($next);
            $missionsProcessed += $this->processDueMissions($players, $errors);
            $cursor = $next;
            if (++$steps > 10000) {
                $errors[] = 'event loop limit reached';
                break;
            }
        }

        // 2) Land on the target time and settle everyone.
        SimClock::write($target);
        $missionsProcessed += $this->processDueMissions($players, $errors);
        $playersUpdated = 0;
        foreach (User::query()->whereHas('tech')->pluck('id') as $userId) {
            try {
                $player = $players->make((int) $userId, true);
                $player->updateResearchQueue(true);
                foreach ($player->planets->all() as $planet) {
                    $planet->update();
                }
                $playersUpdated++;
            } catch (Throwable $e) {
                $errors[] = "player {$userId}: " . $e->getMessage();
            }
        }
        // Missions created as a side effect (e.g. instant returns) may already be due.
        $missionsProcessed += $this->processDueMissions($players, $errors);

        if ($this->option('scores')) {
            Artisan::call('ogamex:scheduler:generate-highscores');
            Artisan::call('ogamex:scheduler:generate-highscore-ranks');
        }

        $this->line(json_encode([
            'from' => $start,
            'to' => $target,
            'event_steps' => $steps,
            'missions_processed' => $missionsProcessed,
            'players_updated' => $playersUpdated,
            'errors' => array_slice($errors, 0, 20),
            'error_count' => count($errors),
        ]));

        return self::SUCCESS;
    }

    /** Earliest unprocessed mission event time in (after, until]. */
    private function nextMissionEvent(int $after, int $until): int|null
    {
        $arrival = FleetMission::query()
            ->where('processed', 0)
            ->where('time_arrival', '>', $after)
            ->where('time_arrival', '<=', $until)
            ->min('time_arrival');
        $holdEnd = FleetMission::query()
            ->where('processed', 0)
            ->whereNotNull('time_holding')
            ->whereRaw('time_arrival + time_holding > ?', [$after])
            ->whereRaw('time_arrival + time_holding <= ?', [$until])
            ->selectRaw('MIN(time_arrival + time_holding) AS t')
            ->value('t');
        $candidates = array_filter([$arrival, $holdEnd], fn ($v) => $v !== null);
        return $candidates === [] ? null : (int) min($candidates);
    }

    /**
     * Process every mission that is due at the current clock by updating the
     * players that own its origin or destination planet.
     *
     * @param array<int, string> $errors
     */
    private function processDueMissions(PlayerServiceFactory $players, array &$errors): int
    {
        $now = SimClock::now();
        $due = FleetMission::query()
            ->where('processed', 0)
            ->where('time_arrival', '<=', $now)
            ->get(['id', 'planet_id_from', 'planet_id_to']);
        if ($due->isEmpty()) {
            return 0;
        }
        $planetIds = $due->pluck('planet_id_from')->merge($due->pluck('planet_id_to'))->filter()->unique();
        $userIds = Planet::query()->whereIn('id', $planetIds)->pluck('user_id')->filter()->unique();
        foreach ($userIds as $userId) {
            try {
                $players->make((int) $userId, true)->updateFleetMissions();
            } catch (Throwable $e) {
                $errors[] = "missions of player {$userId}: " . $e->getMessage();
            }
        }
        $stillDue = FleetMission::query()->whereIn('id', $due->pluck('id'))->where('processed', 0)->count();
        return $due->count() - $stillDue;
    }
}
