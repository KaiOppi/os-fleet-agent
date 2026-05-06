<script type="text/javascript">
$(function () {
    var data_get_map = {'frm_general': '/api/fleet/settings/get'};
    mapDataToFormUI(data_get_map).done(function () {
        formatTokenSection();
    });

    function formatTokenSection() {
        // nothing extra yet — placeholder for token-rotate UI
    }

    function fmtAge(iso) {
        if (!iso) return '<em>never</em>';
        var t = Date.parse(iso);
        if (isNaN(t)) return iso;
        var s = Math.floor((Date.now() - t) / 1000);
        if (s < 60) return s + 's ago';
        if (s < 3600) return Math.floor(s / 60) + 'min ago';
        if (s < 86400) return Math.floor(s / 3600) + 'h ago';
        return Math.floor(s / 86400) + 'd ago';
    }
    function fmtBytes(n) {
        if (!n) return '—';
        if (n > 1048576) return (n / 1048576).toFixed(2) + ' MB';
        if (n > 1024) return (n / 1024).toFixed(1) + ' KB';
        return n + ' B';
    }
    function statusBadge(s) {
        if (s === 'ok')       return '<span class="label label-success">ok</span>';
        if (s === 'disabled') return '<span class="label label-default">disabled</span>';
        if (s)                return '<span class="label label-danger">' + s + '</span>';
        return '—';
    }
    function refreshStatus() {
        ajaxGet('/api/fleet/settings/status', {}, function (data) {
            var rows = [
                ['Enabled',          data.enabled === '1' ? '<span class="label label-success">yes</span>' : '<span class="label label-default">no</span>'],
                ['Server',           data.server_url ? '<code>' + data.server_url + '</code>' : '<em>not set</em>'],
                ['Interval',         (data.interval_minutes || '?') + ' min'],
                ['Last run',         data.last_run ? '<code>' + data.last_run + '</code> <span class="text-muted">(' + fmtAge(data.last_run) + ')</span>' : '<em>never</em>'],
                ['Last status',      statusBadge(data.last_status)],
                ['Last message',     data.last_message || '—'],
                ['Run count',        (data.run_count != null ? data.run_count : '0') + (data.error_count ? ' <span class="label label-danger">' + data.error_count + ' errors</span>' : '')],
                ['Last duration',    data.elapsed_seconds != null ? data.elapsed_seconds + ' s' : '—'],
                ['Last payload',     fmtBytes(data.payload_bytes)],
                ['Last sent counts', data.last_status === 'ok'
                    ? ((data.rules_seen != null ? data.rules_seen : '?') + ' rules · '
                      + (data.plugins_seen != null ? data.plugins_seen : '?') + ' plugins · '
                      + (data.certs_seen != null ? data.certs_seen : '?') + ' certs')
                    : '—'],
                ['Box reports',      data.last_status === 'ok'
                    ? ((data.updates_pending != null ? data.updates_pending : '0') + ' updates pending'
                      + (data.needs_reboot ? ' · <span class="label label-warning">reboot needed</span>' : ''))
                    : '—'],
            ];
            var html = '<table class="table table-condensed"><tbody>';
            rows.forEach(function (r) {
                html += '<tr><th style="width:160px">' + r[0] + '</th><td>' + r[1] + '</td></tr>';
            });
            html += '</tbody></table>';
            $('#fleet-status').html(html);
        });
    }
    refreshStatus();
    setInterval(refreshStatus, 15000);

    $('#saveAct').click(function () {
        saveFormToEndpoint('/api/fleet/settings/set', 'frm_general', function () {
            $('#savedMsg').text('Gespeichert.').show().delay(2500).fadeOut();
            refreshStatus();
        }, true);
    });

    $('#testAct').click(function () {
        $('#testOut').text('Testing …');
        ajaxCall('/api/fleet/settings/test', {}, function (data) {
            $('#testOut').text((data && data.output) ? data.output : 'no output');
        });
    });

    $('#runAct').click(function () {
        $('#testOut').text('Running …');
        ajaxCall('/api/fleet/settings/run', {}, function (data) {
            $('#testOut').text((data && data.output) ? data.output : 'no output');
            refreshStatus();
        });
    });
});
</script>

<div class="content-box">
    <div class="content-box-main">
        <div id="fleet-status" class="table-responsive" style="padding: 10px;"></div>
    </div>
</div>

{{ partial("layout_partials/base_form", ['fields': generalForm, 'id': 'frm_general']) }}

<div class="content-box" style="margin-top: 10px; padding: 10px;">
    <button id="saveAct" type="button" class="btn btn-primary">
        <i class="fa fa-save fa-fw"></i> {{ lang._('Save') }}
    </button>
    <button id="testAct" type="button" class="btn btn-default" style="margin-left: 4px;">
        <i class="fa fa-link fa-fw"></i> Test connection
    </button>
    <button id="runAct" type="button" class="btn btn-default">
        <i class="fa fa-paper-plane fa-fw"></i> Send now
    </button>
    <span id="savedMsg" class="text-success" style="display:none; margin-left: 12px;"></span>
    <pre id="testOut" style="margin-top: 12px; max-height: 240px; overflow:auto;"></pre>
</div>
