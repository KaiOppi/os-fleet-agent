<?php

namespace OPNsense\Fleet\Api;

use OPNsense\Base\ApiMutableModelControllerBase;
use OPNsense\Core\Backend;
use OPNsense\Core\Config;

class SettingsController extends ApiMutableModelControllerBase
{
    protected static $internalModelName = 'fleet';
    protected static $internalModelClass = 'OPNsense\\Fleet\\Fleet';

    public function getAction()
    {
        return parent::getAction();
    }

    public function setAction()
    {
        $result = parent::setAction();
        if ($result['result'] == 'saved') {
            // Re-register the cron entry whenever settings change.
            (new Backend())->configdRun('agent setup');
        }
        return $result;
    }

    public function testAction()
    {
        if (!$this->request->isPost()) {
            return ['result' => 'error', 'message' => 'POST required'];
        }
        $output = (new Backend())->configdRun('agent test');
        return ['result' => 'ok', 'output' => trim($output)];
    }

    public function runAction()
    {
        if (!$this->request->isPost()) {
            return ['result' => 'error', 'message' => 'POST required'];
        }
        $output = (new Backend())->configdRun('agent run');
        return ['result' => 'ok', 'output' => trim($output)];
    }

    public function statusAction()
    {
        $cfg = Config::getInstance()->object();
        $node = $cfg->OPNsense->Fleet->general ?? null;

        $base = [
            'enabled' => isset($node->enabled) ? (string)$node->enabled : '0',
            'server_url' => isset($node->server_url) ? (string)$node->server_url : '',
            'interval_minutes' => isset($node->interval_minutes) ? (string)$node->interval_minutes : '5',
        ];

        // Merge in last-run telemetry written by agent.py.
        $statusFile = '/var/db/os-fleet-agent/status.json';
        if (is_readable($statusFile)) {
            $raw = @file_get_contents($statusFile);
            $j = $raw ? @json_decode($raw, true) : null;
            if (is_array($j)) {
                foreach ([
                    'last_run', 'last_status', 'last_message',
                    'last_ok_run', 'last_error_run',
                    'elapsed_seconds', 'rules_seen', 'plugins_seen',
                    'certs_seen', 'payload_bytes',
                    'needs_reboot', 'updates_pending',
                    'run_count', 'error_count',
                ] as $k) {
                    if (array_key_exists($k, $j)) {
                        $base[$k] = $j[$k];
                    }
                }
            }
        }

        return $base;
    }
}
