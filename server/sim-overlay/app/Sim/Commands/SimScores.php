<?php

namespace OGame\Sim\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use OGame\Sim\SimClock;

/** Dump per-player scores and basic stats as JSON lines (for metrics). */
class SimScores extends Command
{
    protected $signature = 'ogamex:sim:scores';

    protected $description = 'Print per-player highscores and planet counts as JSON';

    public function handle(): int
    {
        $rows = DB::table('users')
            ->leftJoin('highscores', 'highscores.player_id', '=', 'users.id')
            ->select('users.id', 'users.email', 'users.username', 'users.character_class',
                'highscores.general', 'highscores.economy', 'highscores.research', 'highscores.military',
                'highscores.general_rank')
            ->get();
        $planets = DB::table('planets')->where('destroyed', 0)->where('planet_type', 1)
            ->select('user_id', DB::raw('COUNT(*) AS n'))->groupBy('user_id')->pluck('n', 'user_id');
        $this->line(json_encode([
            'clock' => SimClock::read(),
            'players' => $rows->map(fn ($r) => [
                'id' => $r->id,
                'email' => $r->email,
                'class' => $r->character_class,
                'general' => (int) $r->general,
                'economy' => (int) $r->economy,
                'research' => (int) $r->research,
                'military' => (int) $r->military,
                'rank' => $r->general_rank,
                'planets' => (int) ($planets[$r->id] ?? 0),
            ])->values(),
        ]));
        return self::SUCCESS;
    }
}
