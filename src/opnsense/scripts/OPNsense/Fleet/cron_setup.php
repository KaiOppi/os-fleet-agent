<?php
/**
 * os-fleet-agent — register / unregister the cron entry that triggers
 * `configctl agent run` on the configured interval.
 *
 * Called by configctl-action `agent setup`. Idempotent: removes any
 * existing fleet-agent cron entries first, then adds a fresh one if
 * the agent is currently enabled.
 */

require_once 'config.inc';

use OPNsense\Core\Config;

function osf_uuid(): string
{
    $b = random_bytes(16);
    $b[6] = chr((ord($b[6]) & 0x0f) | 0x40);
    $b[8] = chr((ord($b[8]) & 0x3f) | 0x80);
    return vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($b), 4));
}

$cfg = Config::getInstance()->object();

$gen = $cfg->OPNsense->Fleet->general ?? null;
$enabled  = $gen ? ((string)$gen->enabled === '1') : false;
$interval = $gen ? max(1, (int)((string)$gen->interval_minutes ?: '5')) : 5;

// Locate / create the cron job container.
if (!isset($cfg->OPNsense)) { $cfg->addChild('OPNsense'); }
if (!isset($cfg->OPNsense->cron)) {
    $cfg->OPNsense->addChild('cron');
}
if (!isset($cfg->OPNsense->cron->jobs)) {
    $cfg->OPNsense->cron->addChild('jobs');
}

// Remove any existing job whose origin starts with 'os-fleet-agent'.
// Note: unset() on SimpleXMLElement[idx] doesn't reliably remove
// repeated children — DOM-based removal is the supported way.
$jobsNode = $cfg->OPNsense->cron->jobs;
if (isset($jobsNode->job)) {
    $jobsDom = dom_import_simplexml($jobsNode);
    $toRemove = [];
    foreach ($jobsDom->childNodes as $child) {
        if ($child->nodeName !== 'job') {
            continue;
        }
        $origin = '';
        foreach ($child->childNodes as $sub) {
            if ($sub->nodeName === 'origin') {
                $origin = trim($sub->textContent);
                break;
            }
        }
        if (strpos($origin, 'os-fleet-agent') === 0) {
            $toRemove[] = $child;
        }
    }
    foreach ($toRemove as $node) {
        $jobsDom->removeChild($node);
    }
}

if ($enabled) {
    // Build a fresh job. We keep the schedule simple: every N minutes.
    $job = $jobsNode->addChild('job');
    $job->addAttribute('uuid', osf_uuid());
    $job->addChild('enabled', '1');
    $job->addChild('minutes', "*/{$interval}");
    $job->addChild('hours', '*');
    $job->addChild('days', '*');
    $job->addChild('months', '*');
    $job->addChild('weekdays', '*');
    $job->addChild('who', 'root');
    $job->addChild('command', 'agent run');
    $job->addChild('parameters', '');
    $job->addChild('description', 'os-fleet-agent: push status to server');
    $job->addChild('origin', 'os-fleet-agent');
}

Config::getInstance()->save();

// Tell configd to pick up the new cron table.
exec('/usr/local/sbin/configctl cron restart');

if ($enabled) {
    echo "ok: cron registered (every {$interval} min)\n";
} else {
    echo "ok: cron removed (agent disabled)\n";
}
