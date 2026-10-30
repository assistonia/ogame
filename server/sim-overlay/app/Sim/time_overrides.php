<?php

/*
 * PHP resolves an unqualified function call inside a namespace to
 * <namespace>\<function> first, and only then to the global function.
 * Defining time() in the game namespaces that call it routes those calls
 * through the virtual clock without editing upstream files.
 */

namespace OGame\Actions\Fortify {
    function time(): int { return \OGame\Sim\SimClock::now(); }
}

namespace OGame\Http\Controllers {
    function time(): int { return \OGame\Sim\SimClock::now(); }
}

namespace OGame\Http\Controllers\Admin {
    function time(): int { return \OGame\Sim\SimClock::now(); }
}

namespace OGame\Http\Traits {
    function time(): int { return \OGame\Sim\SimClock::now(); }
}

namespace OGame\Models {
    function time(): int { return \OGame\Sim\SimClock::now(); }
}

namespace OGame\Services {
    function time(): int { return \OGame\Sim\SimClock::now(); }
}
