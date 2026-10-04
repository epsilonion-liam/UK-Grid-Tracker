<?php
$DATA_SOURCE_URL = 'https://raw.githubusercontent.com/epsilonion-liam/UK-Grid-Tracker/main/docs/data';
$DATA_CACHE_DIR = rtrim(sys_get_temp_dir(), DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR . 'gb-grid-telemetry-cache';
$PERIODS = [
    'day' => 'Today',
    'previous_day' => 'Previous Day',
    '3days' => '3 Days',
    'week' => 'Week',
    'month' => 'Month',
    'year' => 'Year',
    'previous_year' => 'Previous Year',
];
$PAGES = [
    'overview' => 'Overview',
    'fuelmix' => 'Carbon & Fuel Mix',
    'netzero' => 'Net Zero',
    'documentation' => 'Documentation',
    'changelog' => 'Changelog',
];
$requestedPage = $_GET['page'] ?? '';
$requestedPeriod = $_GET['period'] ?? '';
$requestedRegionalPeriod = $_GET['regional_period'] ?? '';
$page = is_string($requestedPage) && isset($PAGES[$requestedPage]) ? $requestedPage : 'overview';
$period = is_string($requestedPeriod) && isset($PERIODS[$requestedPeriod]) ? $requestedPeriod : 'day';
$regionalPeriod = is_string($requestedRegionalPeriod) && in_array($requestedRegionalPeriod, ['day', 'previous_day'], true)
    ? $requestedRegionalPeriod
    : ($period === 'previous_day' ? 'previous_day' : 'day');

function h(mixed $value)
{
    return htmlspecialchars((string)$value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function decodeGridPayload(string $raw): ?array
{
    $payload = json_decode($raw, true);
    if (!is_array($payload) || !isset($payload['columns'], $payload['rows']) || !is_array($payload['columns']) || !is_array($payload['rows'])) {
        return null;
    }

    $columns = $payload['columns'];
    $rows = [];
    foreach ($payload['rows'] as $row) {
        if (!is_array($row) || count($row) !== count($columns)) {
            continue;
        }
        $rows[] = array_combine($columns, $row);
    }

    return ['rows' => $rows];
}

function requestGridJson(string $url): string|false
{
    if (function_exists('curl_init')) {
        $handle = curl_init($url);
        curl_setopt_array($handle, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 3,
            CURLOPT_TIMEOUT => 7,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_USERAGENT => 'GB-Grid-Telemetry/1.0',
        ]);
        $result = curl_exec($handle);
        $status = (int)curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
        return ($result !== false && $status >= 200 && $status < 300) ? $result : false;
    }

    $context = stream_context_create([
        'http' => [
            'timeout' => 7,
            'follow_location' => 1,
            'header' => "User-Agent: GB-Grid-Telemetry/1.0\r\n",
        ],
    ]);
    return @file_get_contents($url, false, $context);
}

function loadGridDataset(string $name, int $ttl): array
{
    global $DATA_SOURCE_URL, $DATA_CACHE_DIR;

    $cacheFile = $DATA_CACHE_DIR . DIRECTORY_SEPARATOR . $name;
    $cachedRaw = is_file($cacheFile) ? @file_get_contents($cacheFile) : false;
    $cached = ($cachedRaw !== false) ? decodeGridPayload($cachedRaw) : null;
    if ($cached !== null && (time() - (int)@filemtime($cacheFile)) < $ttl) {
        return ['rows' => $cached['rows'], 'stale' => false, 'available' => true];
    }

    $raw = requestGridJson($DATA_SOURCE_URL . '/' . rawurlencode($name));
    $fresh = ($raw !== false) ? decodeGridPayload($raw) : null;
    if ($fresh !== null) {
        if (!is_dir($DATA_CACHE_DIR)) {
            @mkdir($DATA_CACHE_DIR, 0775, true);
        }
        if (is_dir($DATA_CACHE_DIR) && is_writable($DATA_CACHE_DIR)) {
            @file_put_contents($cacheFile, $raw, LOCK_EX);
        }
        return ['rows' => $fresh['rows'], 'stale' => false, 'available' => true];
    }

    if ($cached !== null) {
        return ['rows' => $cached['rows'], 'stale' => true, 'available' => true];
    }

    return ['rows' => [], 'stale' => false, 'available' => false];
}

function loadPreviousRegionalHistory(DateTimeImmutable $day): array
{
    global $DATA_CACHE_DIR;

    $date = $day->format('Y-m-d');
    $cacheFile = $DATA_CACHE_DIR . DIRECTORY_SEPARATOR . 'regional_' . $date . '.json';
    $filterForDay = static function (array $rows) use ($date): array {
        $timezone = new DateTimeZone('Europe/London');
        return array_values(array_filter($rows, static function (array $row) use ($date, $timezone): bool {
            if (empty($row['timestamp'])) {
                return false;
            }
            try {
                return (new DateTimeImmutable((string)$row['timestamp'], new DateTimeZone('UTC')))
                    ->setTimezone($timezone)->format('Y-m-d') === $date;
            } catch (Exception $error) {
                return false;
            }
        }));
    };

    $cachedRaw = is_file($cacheFile) ? @file_get_contents($cacheFile) : false;
    $cached = $cachedRaw !== false ? decodeGridPayload($cachedRaw) : null;
    if ($cached !== null) {
        $cachedRows = $filterForDay($cached['rows']);
        if ($cachedRows) {
            return ['rows' => $cachedRows, 'stale' => false, 'available' => true];
        }
    }

    $dayEnd = $day->modify('+1 day');
    $windowStart = $dayEnd->modify('-45 minutes')->setTimezone(new DateTimeZone('UTC'));
    $windowEnd = $dayEnd->modify('+45 minutes')->setTimezone(new DateTimeZone('UTC'));
    $commitsUrl = 'https://api.github.com/repos/epsilonion-liam/UK-Grid-Tracker/commits?' . http_build_query([
        'path' => 'docs/data/regional_day.json',
        'since' => $windowStart->format(DATE_ATOM),
        'until' => $windowEnd->format(DATE_ATOM),
        'per_page' => 10,
    ]);
    $commitsRaw = requestGridJson($commitsUrl);
    $commits = $commitsRaw !== false ? json_decode($commitsRaw, true) : null;
    if (!is_array($commits)) {
        return ['rows' => [], 'stale' => false, 'available' => false];
    }

    $boundary = $dayEnd->getTimestamp();
    $candidates = [];
    foreach ($commits as $commit) {
        $sha = $commit['sha'] ?? '';
        $commitDate = $commit['commit']['author']['date'] ?? '';
        if (!is_string($sha) || !preg_match('/^[a-f0-9]{40}$/', $sha) || !is_string($commitDate)) {
            continue;
        }
        try {
            $timestamp = (new DateTimeImmutable($commitDate))->getTimestamp();
            $candidates[] = ['sha' => $sha, 'timestamp' => $timestamp];
        } catch (Exception $error) {
            continue;
        }
    }

    $afterBoundary = array_values(array_filter($candidates, static function (array $candidate) use ($boundary): bool {
        return $candidate['timestamp'] >= $boundary;
    }));
    if ($afterBoundary) {
        usort($afterBoundary, static function (array $left, array $right): int {
            return $left['timestamp'] <=> $right['timestamp'];
        });
        $selected = $afterBoundary[0];
    } else {
        $beforeBoundary = array_values(array_filter($candidates, static function (array $candidate) use ($boundary): bool {
            return $candidate['timestamp'] < $boundary;
        }));
        usort($beforeBoundary, static function (array $left, array $right): int {
            return $right['timestamp'] <=> $left['timestamp'];
        });
        $selected = $beforeBoundary[0] ?? null;
    }
    if ($selected === null) {
        return ['rows' => [], 'stale' => false, 'available' => false];
    }

    $raw = requestGridJson('https://raw.githubusercontent.com/epsilonion-liam/UK-Grid-Tracker/' . $selected['sha'] . '/docs/data/regional_day.json');
    $payload = $raw !== false ? decodeGridPayload($raw) : null;
    if ($payload === null) {
        return ['rows' => [], 'stale' => false, 'available' => false];
    }

    if (!is_dir($DATA_CACHE_DIR)) {
        @mkdir($DATA_CACHE_DIR, 0775, true);
    }
    if (is_dir($DATA_CACHE_DIR) && is_writable($DATA_CACHE_DIR)) {
        @file_put_contents($cacheFile, $raw, LOCK_EX);
    }
    $rows = $filterForDay($payload['rows']);
    return ['rows' => $rows, 'stale' => false, 'available' => (bool)$rows];
}

function latestSnapshot(array $rows): array
{
    for ($index = count($rows) - 1; $index >= 0; $index--) {
        if (isset($rows[$index]['total_generation_mw'])) {
            return $rows[$index];
        }
    }
    return $rows ? $rows[count($rows) - 1] : [];
}

function latestAvailableValue(array $rows, string $key): ?array
{
    for ($index = count($rows) - 1; $index >= 0; $index--) {
        $value = $rows[$index][$key] ?? null;
        if ($value !== null && $value !== '' && is_numeric($value)) {
            return [
                'value' => $value,
                'timestamp' => $rows[$index]['timestamp'] ?? null,
            ];
        }
    }
    return null;
}

function rawValue(mixed $value)
{
    return ($value === null || $value === '' || !is_numeric($value)) ? '--' : (string)$value;
}

function formatTruncated(mixed $value, int $decimalPlaces = 2): string
{
    if ($value === null || $value === '' || !is_numeric($value)) {
        return '--';
    }
    $scale = 10 ** $decimalPlaces;
    return number_format((int)((float)$value * $scale) / $scale, $decimalPlaces, '.', '');
}

function formatGw(mixed $value)
{
    return ($value === null || $value === '' || !is_numeric($value)) ? '--' : number_format((float)$value / 1000, 2, '.', '') . ' GW';
}

function formatSharePercent(array $row, array $fields, mixed $demand): string
{
    $value = sumValues($row, $fields);
    if ($value === null || !is_numeric($demand) || (float)$demand <= 0) {
        return '--';
    }
    return formatTruncated((float)$value / (float)$demand * 100) . '%';
}

function sumValues(array $row, array $fields): int|float|null
{
    $total = 0;
    $found = false;
    foreach ($fields as $field) {
        if (isset($row[$field]) && is_numeric($row[$field])) {
            $total += (float)$row[$field];
            $found = true;
        }
    }
    return $found ? $total : null;
}

function formatGridTime(mixed $value, string $format = 'd M H:i')
{
    if (!$value) {
        return '--';
    }
    try {
        $date = new DateTime((string)$value, new DateTimeZone('UTC'));
        $date->setTimezone(new DateTimeZone('Europe/London'));
        return $date->format($format);
    } catch (Exception $error) {
        return '--';
    }
}

function renderLineChart(array $rows, array $series, string $title, string $chartId, int $height = 300): void
{
    if (!$rows) {
        echo '<p class="chart-empty">Chart data is currently unavailable.</p>';
        return;
    }

    $stride = max(1, (int)ceil(count($rows) / 500));
    $points = [];
    for ($index = 0; $index < count($rows); $index += $stride) {
        $points[] = $rows[$index];
    }
    if ($points[count($points) - 1] !== $rows[count($rows) - 1]) {
        $points[] = $rows[count($rows) - 1];
    }

    $groups = ['left' => [], 'right' => []];
    foreach ($series as $item) {
        $axis = isset($item['axis']) && $item['axis'] === 'right' ? 'right' : 'left';
        foreach ($rows as $row) {
            if (isset($row[$item['key']]) && is_numeric($row[$item['key']])) {
                $groups[$axis][] = (float)$row[$item['key']];
            }
        }
    }
    foreach ($groups as $axis => $values) {
        if (!$values) {
            $groups[$axis] = [0, 1];
            continue;
        }
        $minimum = min($values);
        $maximum = max($values);
        if ($minimum === $maximum) {
            $padding = max(abs($minimum) * 0.05, 1);
            $minimum -= $padding;
            $maximum += $padding;
        }
        $groups[$axis] = [$minimum, $maximum];
    }

    $width = 960;
    $plotLeft = 72;
    $plotRight = 900;
    $plotTop = 22;
    $plotBottom = $height - 42;
    $plotWidth = $plotRight - $plotLeft;
    $plotHeight = $plotBottom - $plotTop;
    $titleId = $chartId . '-title';
    $descId = $chartId . '-desc';
    $firstTime = isset($points[0]['timestamp']) ? formatGridTime($points[0]['timestamp']) : '--';
    $lastTime = isset($points[count($points) - 1]['timestamp']) ? formatGridTime($points[count($points) - 1]['timestamp']) : '--';
    $chartData = ['timestamps' => [], 'values' => []];
    foreach ($series as $item) {
        $chartData['values'][] = [];
    }
    foreach ($rows as $row) {
        try {
            $chartData['timestamps'][] = isset($row['timestamp'])
                ? (new DateTime((string)$row['timestamp'], new DateTimeZone('UTC')))->getTimestamp()
                : null;
        } catch (Exception $error) {
            $chartData['timestamps'][] = null;
        }
        foreach ($series as $index => $item) {
            $value = $row[$item['key']] ?? null;
            $chartData['values'][$index][] = is_numeric($value) ? (float)$value : null;
        }
    }
    $chartConfig = json_encode(
        ['data' => $chartData, 'series' => $series, 'height' => $height],
        JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT
    );

    echo '<figure class="server-chart">';
    echo '<div class="interactive-chart-host" aria-label="' . h($title) . ' interactive chart"></div>';
    echo '<script type="application/json" class="interactive-chart-data">' . $chartConfig . '</script>';
    echo '<svg class="chart-svg" viewBox="0 0 ' . $width . ' ' . (int)$height . '" role="img" aria-labelledby="' . h($titleId) . ' ' . h($descId) . '">';
    echo '<title id="' . h($titleId) . '">' . h($title) . '</title>';
    echo '<desc id="' . h($descId) . '">Time series from ' . h($firstTime) . ' to ' . h($lastTime) . ', rendered from server-provided data.</desc>';

    for ($tick = 0; $tick <= 4; $tick++) {
        $y = $plotTop + ($plotHeight * $tick / 4);
        echo '<line class="chart-gridline" x1="' . $plotLeft . '" y1="' . number_format($y, 2, '.', '') . '" x2="' . $plotRight . '" y2="' . number_format($y, 2, '.', '') . '" />';
        foreach (['left', 'right'] as $axis) {
            $seriesOnAxis = array_values(array_filter($series, function (array $item) use ($axis): bool {
                return (($item['axis'] ?? 'left') === $axis);
            }));
            if (!$seriesOnAxis) {
                continue;
            }
            list($minimum, $maximum) = $groups[$axis];
            $value = $maximum - (($maximum - $minimum) * $tick / 4);
            $labelX = $axis === 'left' ? 64 : 908;
            $anchor = $axis === 'left' ? 'end' : 'start';
            $precision = abs($maximum - $minimum) < 10 ? 2 : 0;
            echo '<text class="chart-axis-label" x="' . $labelX . '" y="' . number_format($y + 4, 2, '.', '') . '" text-anchor="' . $anchor . '">' . h(number_format($value, $precision, '.', '')) . '</text>';
        }
    }

    foreach ($series as $item) {
        $axis = isset($item['axis']) && $item['axis'] === 'right' ? 'right' : 'left';
        list($minimum, $maximum) = $groups[$axis];
        $commands = [];
        $started = false;
        foreach ($points as $index => $row) {
            $value = $row[$item['key']] ?? null;
            if (!is_numeric($value)) {
                $started = false;
                continue;
            }
            $x = $plotLeft + ($plotWidth * $index / max(count($points) - 1, 1));
            $y = $plotBottom - (((float)$value - $minimum) / ($maximum - $minimum) * $plotHeight);
            $commands[] = ($started ? 'L' : 'M') . number_format($x, 2, '.', '') . ' ' . number_format($y, 2, '.', '');
            $started = true;
        }
        if ($commands) {
            echo '<path class="chart-line" d="' . h(implode(' ', $commands)) . '" stroke="' . h($item['color']) . '" />';
        }
    }

    echo '<text class="chart-axis-label" x="' . $plotLeft . '" y="' . ($height - 12) . '" text-anchor="start">' . h($firstTime) . '</text>';
    echo '<text class="chart-axis-label" x="' . $plotRight . '" y="' . ($height - 12) . '" text-anchor="end">' . h($lastTime) . '</text>';
    echo '</svg><figcaption><ul class="chart-legend">';
    foreach ($series as $item) {
        echo '<li><span class="chart-key" style="--series-color:' . h($item['color']) . '"></span>' . h($item['label']) . '</li>';
    }
    echo '</ul><span class="chart-range">' . h($firstTime) . ' to ' . h($lastTime) . '</span><span class="chart-interaction-hint">Hover for values | drag to zoom | double-click to reset</span></figcaption></figure>';
}

function regionColor(string $level): string
{
    $colors = [
        'very low' => 'var(--very-low)',
        'low' => 'var(--low)',
        'moderate' => 'var(--moderate)',
        'high' => 'var(--high)',
        'very high' => 'var(--very-high)',
    ];
    return $colors[strtolower((string)$level)] ?? 'var(--border)';
}

function fuelColor(string $fuel): string
{
    $colors = [
        'biomass' => 'var(--c-biomass)',
        'coal' => 'var(--c-coal)',
        'gas' => 'var(--c-gas)',
        'hydro' => 'var(--c-hydro)',
        'imports' => 'var(--c-imports)',
        'nuclear' => 'var(--c-nuclear)',
        'solar' => 'var(--c-solar)',
        'wind' => 'var(--c-wind)',
    ];
    return $colors[strtolower(trim($fuel))] ?? 'var(--border)';
}

function dashboardUrl(string $page, string $period, string $regionalPeriod = 'day'): string
{
    return '?' . http_build_query(['page' => $page, 'period' => $period, 'regional_period' => $regionalPeriod]);
}

$timeline = loadGridDataset($period . '.json', $period === 'day' ? 60 : 1800);
$rows = $timeline['rows'];
$snapshot = latestSnapshot($rows);
$carbonIntensityReading = latestAvailableValue($rows, 'carbon_intensity');
$wholesalePriceReading = latestAvailableValue($rows, 'wholesale_price_gbp_mwh');
$regions = ($page === 'changelog') ? ['rows' => [], 'stale' => false, 'available' => true] : loadGridDataset('regional_latest.json', 180);
$regionalHistory = ['rows' => [], 'stale' => false, 'available' => true];
if ($page === 'fuelmix') {
    if ($regionalPeriod === 'previous_day') {
        $previousRegionalDate = (new DateTimeImmutable('today', new DateTimeZone('Europe/London')))->modify('-1 day');
        $regionalHistory = loadPreviousRegionalHistory($previousRegionalDate);
    } else {
        $regionalHistory = loadGridDataset('regional_day.json', 300);
    }
}
$forecast = ($page === 'overview') ? loadGridDataset('carbon_forecast.json', 300) : ['rows' => [], 'stale' => false, 'available' => true];
$dataIsStale = $timeline['stale'] || $regions['stale'] || $regionalHistory['stale'] || $forecast['stale'];
$hasUnavailableData = !$timeline['available'] || !$regions['available'] || !$regionalHistory['available'] || !$forecast['available'];
$latestTimestamp = $snapshot['timestamp'] ?? null;
$isOutdated = false;
if ($period === 'day' && $latestTimestamp) {
    try {
        $isOutdated = (time() - (new DateTime((string)$latestTimestamp, new DateTimeZone('UTC')))->getTimestamp()) > 4500;
    } catch (Exception $error) {
        $isOutdated = false;
    }
}
?>
<!DOCTYPE html>
<html lang="en-GB">
<?php
ini_set('display_errors', 1);
ini_set('error_reporting', E_ALL);
?>

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GB Electricity Grid Tracker: Live Generation & Carbon Data | UK Politics Decoded</title>
    <meta name="description" content="Explore Great Britain's live electricity generation mix, demand, carbon intensity, wholesale prices, system frequency and regional data with this free dashboard from UK Politics Decoded.">
    <meta name="author" content="UK Politics Decoded">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="https://nationalgrid.ukpoliticsdecoded.uk/">
    <link rel="icon" href="https://ukpoliticsdecoded.uk/favicon.ico" type="image/webp">

    <!-- Open Graph / social previews -->
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="UK Politics Decoded">
    <meta property="og:url" content="https://nationalgrid.ukpoliticsdecoded.uk/">
    <meta property="og:locale" content="en_GB">
    <meta property="og:title" content="GB Electricity Grid Tracker: Live Generation &amp; Carbon Data">
    <meta property="og:description" content="Explore Great Britain's live electricity generation mix, demand, carbon intensity, wholesale prices, system frequency and regional data with this free dashboard from UK Politics Decoded.">
    <meta property="og:image" content="https://ukpoliticsdecoded.uk/assets/images/logo.webp">
    <meta property="og:image:type" content="image/webp">
    <meta property="og:image:alt" content="UK Politics Decoded">

    <!-- X/Twitter social previews -->
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="GB Electricity Grid Tracker: Live Generation &amp; Carbon Data">
    <meta name="twitter:description" content="Explore Great Britain's live electricity generation mix, demand, carbon intensity, wholesale prices, system frequency and regional data with this free dashboard from UK Politics Decoded.">
    <meta name="twitter:image" content="https://ukpoliticsdecoded.uk/assets/images/logo.webp">
    <meta name="twitter:image:alt" content="UK Politics Decoded">

    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {
          "@type": "WebApplication",
          "@id": "https://nationalgrid.ukpoliticsdecoded.uk/#webapplication",
          "name": "GB National Grid Live Telemetry",
          "alternateName": "GB Electricity Grid Tracker",
          "url": "https://nationalgrid.ukpoliticsdecoded.uk/",
          "dateCreated": "2026-09-28",
          "description": "A free dashboard from UK Politics Decoded for exploring Great Britain's electricity generation mix, demand, carbon intensity, wholesale prices, system frequency and regional data.",
          "applicationCategory": "UtilitiesApplication",
          "operatingSystem": "Any",
          "isAccessibleForFree": true,
          "inLanguage": "en-GB",
          "about": {
            "@type": "Thing",
            "name": "Great Britain's electricity grid"
          },
          "publisher": {
            "@id": "https://ukpoliticsdecoded.uk/#publisher"
          },
          "creator": {
            "@id": "https://ukpoliticsdecoded.uk/#publisher"
          },
          "isPartOf": {
            "@type": "WebSite",
            "@id": "https://ukpoliticsdecoded.uk/#website",
            "name": "UK Politics Decoded",
            "url": "https://ukpoliticsdecoded.uk/",
            "publisher": {
              "@id": "https://ukpoliticsdecoded.uk/#publisher"
            }
          }
        },
        {
          "@type": "BreadcrumbList",
          "@id": "https://nationalgrid.ukpoliticsdecoded.uk/#breadcrumb",
          "itemListElement": [
            {
              "@type": "ListItem",
              "position": 1,
              "name": "UK Politics Decoded",
              "item": "https://ukpoliticsdecoded.uk/"
            },
            {
              "@type": "ListItem",
              "position": 2,
              "name": "GB National Grid Live Telemetry",
              "item": "https://nationalgrid.ukpoliticsdecoded.uk/"
            }
          ]
        }
      ]
    }
    </script>

    <!-- External CSS -->
    <link rel="stylesheet" href="styles.css?v=<?php echo time(); ?>">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css">

</head>

<body>
<div id="app" itemscope itemtype="https://schema.org/WebApplication">
    <meta itemprop="name" content="GB National Grid Live Telemetry">
    <meta itemprop="url" content="https://nationalgrid.ukpoliticsdecoded.uk/">
    <meta itemprop="description" content="A free dashboard from UK Politics Decoded for exploring Great Britain's electricity generation mix, demand, carbon intensity, wholesale prices, system frequency and regional data.">
    <meta itemprop="applicationCategory" content="UtilitiesApplication">
    <meta itemprop="operatingSystem" content="Any">
    <meta itemprop="isAccessibleForFree" content="true">
    <meta itemprop="inLanguage" content="en-GB">

    <!-- Top Bar -->
    <header class="top-bar">
        <h1>GB National Grid Live <span>Telemetry</span></h1>
        <div class="header-links">
            <a href="https://ukpoliticsdecoded.uk/" target="_blank" rel="noopener noreferrer" class="header-link-btn">
                <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M10 20v-6h4v6h5V6h-7l-8 8v6h5z"/></svg>
                UK Politics Decoded
            </a>
            <a href="https://github.com/epsilonion-liam/UK-Grid-Tracker" target="_blank" rel="noopener noreferrer" class="header-link-btn">
                <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill-rule="evenodd" clip-rule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.485.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.399-1.988 1.029-2.688-.103-.253-.446-1.272.096-2.647 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.542 1.375.201 2.394.1 2.647.647.7 1.034 1.592 1.028 2.688 0 3.842-2.339 4.687-4.564 4.935.359.31.698 1.028.698 2.082 0 1.537-.01 2.783-.01 3.196 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"/></svg>
                GitHub Repo
            </a>
        </div>
        
        <?php if ($hasUnavailableData || $dataIsStale || $isOutdated): ?>
        <div id="outage-banner" class="outage-banner visible" role="status">
            <span class="icon">⚠️</span>
            <div>
                <strong><?php echo $hasUnavailableData ? 'Data source unavailable' : 'Data source delayed'; ?></strong><br>
                <span class="detail"><?php
                    if (!$timeline['available']) {
                        echo 'The selected dataset could not be fetched and no cached copy is available.';
                    } elseif ($hasUnavailableData) {
                        echo 'One or more datasets could not be fetched; unavailable sections are marked below.';
                    } elseif ($dataIsStale) {
                        echo 'Showing the last cached reading because one or more upstream datasets could not be refreshed.';
                    } else {
                        echo 'The latest complete grid reading is older than 75 minutes.';
                    }
                ?></span>
            </div>
        </div>
        <?php endif; ?>
    </header>

    <!-- Metrics Bar -->
    <section class="metrics-bar">
        <div id="metric-intensity" class="metric-chip">
            <span class="label">Carbon Intensity</span>
            <div class="value" id="val-intensity"<?php echo $carbonIntensityReading && !empty($carbonIntensityReading['timestamp']) ? ' title="Latest available reading: ' . h(formatGridTime($carbonIntensityReading['timestamp'])) . '"' : ''; ?>><span><?php echo h(rawValue($carbonIntensityReading['value'] ?? null)); ?></span><span class="unit">gCO₂/kWh</span></div>
        </div>
        <div id="metric-demand" class="metric-chip">
            <span class="label">System Demand</span>
            <div class="value" id="val-demand"><span><?php echo h(rawValue(isset($snapshot['total_demand_mw']) && is_numeric($snapshot['total_demand_mw']) ? number_format((float)$snapshot['total_demand_mw'] / 1000, 2, '.', '') : null)); ?></span><span class="unit">GW</span></div>
        </div>
        <div id="metric-frequency" class="metric-chip">
            <span class="label">Freq. (Hz)</span>
            <div class="value" id="val-frequency"><span><?php echo h(formatTruncated($snapshot['system_frequency'] ?? null)); ?></span><span class="unit">Hz</span></div>
        </div>
        <div id="metric-generation" class="metric-chip">
            <span class="label">Total Gen.</span>
            <div class="value" id="val-generation"><span><?php echo h(rawValue(isset($snapshot['total_generation_mw']) && is_numeric($snapshot['total_generation_mw']) ? number_format((float)$snapshot['total_generation_mw'] / 1000, 2, '.', '') : null)); ?></span><span class="unit">GW</span></div>
        </div>
        <div id="metric-transfers" class="metric-chip">
            <span class="label">Net Transfers</span>
            <div class="value" id="val-transfers"><span><?php echo h(rawValue(isset($snapshot['total_net_transfer_mw']) && is_numeric($snapshot['total_net_transfer_mw']) ? number_format((float)$snapshot['total_net_transfer_mw'] / 1000, 2, '.', '') : null)); ?></span><span class="unit">GW</span></div>
        </div>
        <div id="metric-price" class="metric-chip">
            <span class="label">Wholesale Price</span>
            <div class="value" id="val-price"<?php echo $wholesalePriceReading && !empty($wholesalePriceReading['timestamp']) ? ' title="Latest available reading: ' . h(formatGridTime($wholesalePriceReading['timestamp'])) . '"' : ''; ?>><span><?php echo h(rawValue($wholesalePriceReading['value'] ?? null)); ?></span><span class="unit">£/MWh</span></div>
        </div>
        <div id="metric-time" class="metric-chip">
            <span class="label">Last Update</span>
            <div class="value" id="val-time"><span><?php echo h(formatGridTime($latestTimestamp, 'H:i')); ?></span><span class="unit"><?php echo h(formatGridTime($latestTimestamp, 'T')); ?></span></div>
        </div>
    </section>

    <!-- Navigation -->
    <nav class="page-nav" aria-label="Dashboard pages">
        <?php foreach ($PAGES as $key => $label): ?>
        <a class="<?php echo $page === $key ? 'active' : ''; ?>" href="<?php echo h(dashboardUrl($key, $period, $regionalPeriod)); ?>"<?php echo $page === $key ? ' aria-current="page"' : ''; ?>><?php echo h($label); ?></a>
        <?php endforeach; ?>
    </nav>

    <?php if ($page === 'overview' || $page === 'fuelmix'): ?>
    <nav class="history-nav" aria-label="Data period">
        <?php foreach ($PERIODS as $key => $label): ?>
        <a class="<?php echo $period === $key ? 'active' : ''; ?>" href="<?php echo h(dashboardUrl($page, $key, $regionalPeriod)); ?>"<?php echo $period === $key ? ' aria-current="page"' : ''; ?>><?php echo h($label); ?></a>
        <?php endforeach; ?>
        <span class="nav-status"><?php echo count($rows); ?> readings</span>
    </nav>
    <?php endif; ?>

    <!-- Main Chart Area -->
    <main class="viewport">
        <?php if ($page === 'overview'): ?>
        <section id="page-overview" class="app-page active">
            <div class="chart-panel">
                <div class="panel-title"><span>System Balancing Volume & Frequency (LIVE)</span></div>
                <div class="chart-holder">
                    <?php renderLineChart($rows, [
                        ['key' => 'balancing_volume_mw', 'label' => 'Balancing Volume (MW)', 'color' => '#f2994a', 'axis' => 'left'],
                        ['key' => 'system_frequency', 'label' => 'System Frequency (Hz)', 'color' => '#4fd1ff', 'axis' => 'right'],
                    ], 'System Balancing Volume and Frequency', 'overview-chart', 260); ?>
                </div>
            </div>
        </section>
        <?php elseif ($page === 'fuelmix'): ?>
        <section id="page-carbon-system" class="app-page active">
            <div class="chart-panel">
                <div class="panel-title"><span>National Generation Mix & Carbon Intensity (LIVE)</span></div>
                <div class="chart-holder">
                    <?php renderLineChart($rows, [
                        ['key' => 'wind_mw', 'label' => 'Wind (MW)', 'color' => '#45c4a0'],
                        ['key' => 'solar_mw', 'label' => 'Solar (MW)', 'color' => '#f2c94c'],
                        ['key' => 'gas_mw', 'label' => 'Gas (MW)', 'color' => '#e3654b'],
                        ['key' => 'nuclear_mw', 'label' => 'Nuclear (MW)', 'color' => '#b085f5'],
                        ['key' => 'biomass_mw', 'label' => 'Biomass (MW)', 'color' => '#a3823a'],
                        ['key' => 'imports_mw', 'label' => 'Imports (MW)', 'color' => '#6c8ebf'],
                        ['key' => 'carbon_intensity', 'label' => 'Carbon Intensity (gCO₂/kWh)', 'color' => '#ffffff', 'axis' => 'right'],
                    ], 'National Generation Mix and Carbon Intensity', 'fuelmix-chart', 300); ?>
                </div>
            </div>
            
            <div class="chart-panel">
                <div class="panel-title"><span>Wholesale Electricity Price (LIVE)</span></div>
                <div class="chart-holder">
                    <?php renderLineChart($rows, [
                        ['key' => 'wholesale_price_gbp_mwh', 'label' => 'Wholesale Price (£/MWh)', 'color' => '#f2c94c'],
                    ], 'Wholesale Electricity Price', 'price-chart', 240); ?>
                </div>
            </div>
        </section>
        <?php elseif ($page === 'netzero'): ?>
        <section id="page-netzero" class="documentation-page netzero-page">
            <h2>Net Zero, Electricity and the Changing Grid</h2>
            <p class="documentation-intro">A guide to what the dashboard's electricity data can tell us about the energy transition, and what it cannot.</p>

            <section class="documentation-section">
                <h3>Generation mix is not the same as total energy use</h3>
                <p>The generation mix shows the sources producing electricity during a given period, such as wind, solar, gas, nuclear and biomass. Great Britain also imports and exports electricity through interconnectors, and storage can shift electricity between periods. Imports are shown as transfers; the generation mix in the exporting country is not identified in these figures.</p>
                <p>This dashboard covers the electricity system. It does not show all energy consumed for transport, heating or industrial processes, much of which may still come directly from fuels rather than electricity. The mix also changes through the day as weather, demand, plant availability and network constraints change.</p>
            </section>

            <section class="documentation-section">
                <h3>How electrification may change the mix</h3>
                <p>As vehicles, building heating and some industrial processes switch to electricity, electricity demand is expected to grow. The amount and timing of that growth depend on technology, policy, efficiency and how flexibly new demand can be scheduled.</p>
                <p>More wind and solar can reduce fossil-fuel generation when their output is available, but their output varies with weather and time of day. A reliable, lower-carbon system therefore also needs a diverse mix of resources: firm low-carbon generation, storage, interconnectors, flexible demand and enough transmission and distribution capacity. Gas generation may continue to help balance the system during periods when low-carbon output is insufficient; its future role depends on the pace of investment and the wider energy transition.</p>
                <p>More diverse supply can reduce exposure to any one fuel or import route, but it does not remove all price volatility or guarantee lower bills. Weather, outages, fuel markets, network constraints and the cost of new infrastructure can all affect system costs.</p>
            </section>

            <section class="documentation-section">
                <h3>Marginal pricing and possible market changes</h3>
                <p>In the wholesale electricity market, the price for a settlement period is generally influenced by the most expensive generation needed to meet demand at that time. This is called marginal pricing. It helps coordinate supply and demand, but when gas is the marginal generator, movements in gas prices can influence the wholesale price paid across the market, including for electricity generated by other sources.</p>
                <p>Market reforms are debated, including changes to how prices reflect location and network constraints, and greater use of long-term contracts. These options involve trade-offs for investment, reliability, consumers and system operation. The wholesale price shown on this dashboard is a market reading; it is not a measure of the marginal-pricing system's share of a household bill.</p>
                <p>Changing the pricing mechanism would not automatically produce household savings. Any claimed saving needs to be assessed against the full costs of generation, balancing, networks, storage, backup and transition, as well as how costs and benefits are passed through to consumers.</p>
            </section>

            <section class="documentation-section">
                <h3>Household bills: estimates need context</h3>
                <p>Wholesale energy costs and policy-related costs are only parts of a household electricity bill; bills also include network charges, operating costs, taxes and supplier costs. Estimates that assign a share of bills to policy costs or wholesale energy can vary by year, tariff, customer usage and accounting method. The percentage of a bill attributed to wholesale energy is not the same as the amount that could be saved by replacing marginal pricing.</p>
                <p>For that reason, any figures such as an estimated 8–15% for policy-related costs or roughly one-third for wholesale energy should be treated as dated estimates, not fixed or universal shares. This dashboard does not calculate household bills or forecast savings from market reform.</p>
            </section>

            <section class="documentation-section">
                <h3>Grid connections and the build-out</h3>
                <p>New generation and storage need suitable grid connections, and the connections queue has been a major delivery challenge. Government and regulators have announced changes intended to prioritise viable projects and speed up network delivery. These are plans and reforms in progress; their implementation and effect on individual project timelines will depend on delivery.</p>
                <p>Read more about the announced Great British Grid and connections measures in <a href="https://ukpoliticsdecoded.uk/decoded-news/2026/great-british-grid-connections-bills-2026.html" target="_blank" rel="noopener noreferrer">Great British Grid, connections and bills</a>. The announcement describes the intended benefits, but does not quantify guaranteed household bill savings.</p>
            </section>

            <section class="documentation-section">
                <h3>What to look for in the dashboard</h3>
                <ul>
                    <li>Compare the wind, solar, gas and other generation series across different periods to see how the observed mix changes.</li>
                    <li>Use the carbon-intensity readings as an indicator of emissions associated with electricity, not as a direct measure of household bills or all energy use.</li>
                    <li>Check the reading timestamps: source feeds update at different times, and recent readings may be incomplete or delayed.</li>
                    <li>Remember that half-hourly observations describe the system at those times; they are not long-term forecasts of the future generation mix.</li>
                </ul>
            </section>
        </section>
        <?php elseif ($page === 'documentation'): ?>
        <section id="page-documentation" class="documentation-page">
            <h2>Dashboard Documentation</h2>
            <p class="documentation-intro">How to read the GB electricity and carbon data, where it comes from, and what its limitations are.</p>

            <section class="documentation-section" id="data-safeguard">
                <h3>Data safeguard: why some readings are delayed or missing</h3>
                <p>This dashboard combines three independently updated sources, Elexon <code>FUELINST</code> generation and interconnector readings, Elexon Market Index Data for wholesale prices, and the Carbon Intensity API. These sources do not always publish their readings at the same time or for the same settlement period.</p>
                <p>To avoid presenting an incomplete fuel mix as a complete one, the generation, demand, and decarbonisation summary uses the latest settlement period with a total generation reading. Carbon intensity and wholesale price can update on a different schedule, their summary values show the latest available reading, and hovering over either value shows its reading time. A gap in a chart means that source has no value for that period.</p>
                <p>If a feed is delayed, the dashboard may continue to show the latest available or completed readings. A delay does not necessarily mean the electricity system itself is unstable.</p>
            </section>

            <section class="documentation-section" id="accuracy">
                <h3>Is this dashboard accurate?</h3>
                <p>It is a best effort view of published grid data, not a direct meter for every generator or a guaranteed real time measurement. Gas, coal, nuclear, biomass, hydro, and interconnector flows come directly from Elexon's <code>FUELINST</code> readings for transmission connected generation and flows.</p>
                <p>Solar is not metered by Elexon, and its wind readings cover transmission connected wind farms only. Those figures are supplemented with NESO's Demand Data Update estimates of embedded generation connected to distribution networks, such as rooftop solar. As a result, embedded wind and solar values are estimates.</p>
                <p>Battery storage may show <code>--</code> because Elexon does not currently publish the battery charge/discharge data needed for this breakdown. Fuel percentages are calculated relative to total demand (generation plus net imports and storage discharge, minus storage charging) and may not add up to exactly 100% because of estimates, rounding, and unavailable readings.</p>
                <p><strong>Why can figures differ from other dashboards, such as <a href="https://grid.iamkate.com/" target="_blank" rel="noopener noreferrer">grid.iamkate.com</a>?</strong> Dashboards use the same public Elexon and NESO feeds, so their figures should be broadly similar, but may differ because they combine embedded generation differently, calculate demand independently, and refresh on different schedules. Comparing dashboards is comparing independent snapshots and estimates of the same grid, not necessarily identical readings.</p>
            </section>

            <section class="documentation-section" id="reading-dashboard">
                <h3>Reading the dashboard</h3>
                <ul>
                    <li><strong>Overview:</strong> system balancing volume and frequency over time, with current summary metrics and grid operations.</li>
                    <li><strong>Carbon &amp; Fuel Mix:</strong> national generation by fuel, carbon intensity, wholesale price, and the fuel and transfer breakdown.</li>
                    <li><strong>Regional carbon intensity:</strong> the first 24 hour heatmap is coloured by carbon intensity category. Hover over a cell for its time, dominant fuel, exact gCO₂/kWh value, and category.</li>
                    <li><strong>Regional primary fuel:</strong> the second heatmap uses a separate colour for each primary fuel. Its legend identifies the fuel, and hovering over a cell shows the fuel and the reading details.</li>
                </ul>
                <p>Charts use half hour settlement period readings where available, aligning them with Great Britain's half hourly wholesale marginal pricing periods. Hover over an interactive chart to inspect values, drag across it to zoom, and double click to reset the time range. Values are reported in the units shown on each chart or metric, GW is generation or demand in MW divided by 1,000.</p>
                <p>Use the period links to view Today, Previous Day, 3 Days, Week, Month, Year, or Previous Year. The regional heatmaps have their own selector for the most recent 24 hours or the previous calendar day.</p>
            </section>

            <section class="documentation-section" id="terms">
                <h3>Imports, exports, and prices</h3>
                <p>Interconnector values are shown from Great Britain's perspective, positive values indicate imports and negative values indicate exports. The wholesale price is a system wide market price for a settlement period, not a separate price for each fuel type.</p>
            </section>

            <section class="documentation-section" id="public-datasets">
                <h3>Public JSON datasets</h3>
                <p>The dashboard's exported datasets are available as JSON files in the public <a href="https://github.com/epsilonion-liam/UK-Grid-Tracker/tree/main/docs/data" target="_blank" rel="noopener noreferrer">UK Grid Tracker GitHub repository</a>. They can be downloaded and used by anyone, no dashboard account is required. The repository includes national time series for different periods, regional readings, and carbon intensity forecasts.</p>
                <p>Time series files use a compact table format, <code>columns</code> lists the field names, and each item in <code>rows</code> contains the corresponding values in that column order. The files can be inspected in a browser, downloaded, or read by scripts and other applications.</p>
                <p>The JSON exports combine data from upstream providers. If you reuse or redistribute them, check and follow the providers' applicable terms, licences, and attribution, in particular, see <a href="https://www.elexon.co.uk/data/balancing-mechanism-reporting-agent/copyright-licence-bmrs-data/" target="_blank" rel="noopener noreferrer">Elexon's BMRS data licence</a>.</p>
            </section>

            <section class="documentation-section" id="data-sources">
                <h3>Data sources and contact</h3>
                <ul>
                    <li><a href="https://bmrs.elexon.co.uk/" target="_blank" rel="noopener noreferrer">Elexon Insights Solution</a> - generation, system, and market data.</li>
                    <li><a href="https://www.neso.energy/data-portal" target="_blank" rel="noopener noreferrer">National Energy System Operator (NESO) Data Portal</a> - demand data and embedded generation estimates.</li>
                    <li><a href="https://carbonintensity.org.uk/" target="_blank" rel="noopener noreferrer">Carbon Intensity API</a> - carbon intensity data.</li>
                </ul>
                <p>For questions or suggestions, contact <a href="mailto:support@ukpoliticsdecoded.uk">support@ukpoliticsdecoded.uk</a>. This dashboard is an independent informational project and is not an official government or system operator service.</p>
            </section>
        </section>
        <?php elseif ($page === 'changelog'): ?>
        <section id="page-changelog" class="app-page changelog-page active">
            <section class="changelog">
                <h2>Changelog</h2>
                <p class="changelog-note">A running log of notable updates to this dashboard. Suggestions are welcome at <a href="mailto:support@ukpoliticsdecoded.uk">support@ukpoliticsdecoded.uk</a>.</p>
                <div class="changelog-list">
                    <article class="changelog-entry">
                        <div class="changelog-date">2026-09-28</div>
                        <h3>Changelog tab added</h3>
                        <ul>
                            <li>Added the changelog and data freshness guard.</li>
                            <li>Added system frequency, national fuel mix, and wholesale price charts.</li>
                            <li>Added regional carbon intensity data for all 14 DNO regions.</li>
                        </ul>
                    </article>
                </div>
            </section>
        </section>
        <?php endif; ?>
    </main>

    <?php if ($page === 'overview'): ?>
    <!-- Summary Cards -->
    <section class="summary">
        <div class="summary-cards">
            <div class="summary-card" id="decarbCard">
                <h3>Grid Decarbonisation</h3>
                <div class="decarb-body">
                    <div class="gauge-wrap">
                        <svg class="gauge-svg" viewBox="0 0 120 120">
                            <circle class="gauge-track" cx="60" cy="60" r="50"></circle>
                            <?php $zeroCarbon = $snapshot['zero_carbon_share_pct'] ?? null; $zeroCarbonClamped = is_numeric($zeroCarbon) ? max(0, min(100, (float)$zeroCarbon)) : 0; $zeroCarbonDisplay = is_numeric($zeroCarbon) ? formatTruncated($zeroCarbon) : null; ?>
                            <circle class="gauge-progress" cx="60" cy="60" r="50" style="stroke-dashoffset:<?php echo number_format(314.159 * (1 - $zeroCarbonClamped / 100), 3, '.', ''); ?>"></circle>
                        </svg>
                        <div class="gauge-label">
                            <span class="pct"><?php echo $zeroCarbonDisplay !== null ? h($zeroCarbonDisplay) . '%' : '--%'; ?></span>
                            <span class="caption">Zero-Carbon</span>
                        </div>
                    </div>
                    <div class="decarb-submetrics">
                        <div class="submetric-row low-carbon">
                            <span class="name">Low-Carbon Output</span>
                            <span class="val"><?php echo h(formatGw($snapshot['zero_carbon_mw'] ?? null)); ?></span>
                        </div>
                        <div class="submetric-row fossil">
                            <span class="name">Fossil Fuel Output</span>
                            <span class="val"><?php echo h(formatGw(sumValues($snapshot, ['gas_mw', 'coal_mw']))); ?></span>
                        </div>
                        <div class="submetric-row low-carbon">
                            <span class="name">Renewables Share</span>
                            <span class="val"><?php echo h(formatSharePercent($snapshot, ['wind_mw', 'solar_mw', 'hydro_mw'], $snapshot['total_demand_mw'] ?? null)); ?></span>
                        </div>
                        <div class="submetric-row fossil">
                            <span class="name">Fossil Fuel Share</span>
                            <span class="val"><?php echo h(formatSharePercent($snapshot, ['gas_mw', 'coal_mw'], $snapshot['total_demand_mw'] ?? null)); ?></span>
                        </div>
                    </div>
                </div>
                <div class="sparkline-holder">
                    <?php renderLineChart($forecast['rows'], [
                        ['key' => 'forecast_intensity', 'label' => 'Carbon Intensity Forecast (gCO₂/kWh)', 'color' => '#ffffff'],
                    ], '48-hour Carbon Intensity Forecast', 'forecast-chart', 100); ?>
                </div>
            </div>

            <div class="summary-card" id="opsCard">
                <h3>Grid Operations</h3>
                <div class="ops-body">
                    <div class="freq-display">
                        <span class="freq-value"><?php echo h(formatTruncated($snapshot['system_frequency'] ?? null)); ?> Hz</span>
                        <?php $frequencyDelta = $snapshot['freq_delta'] ?? null; ?>
                        <span class="freq-delta <?php echo !is_numeric($frequencyDelta) || (float)$frequencyDelta === 0.0 ? 'flat' : ((float)$frequencyDelta > 0 ? 'rising' : 'falling'); ?>"><?php echo is_numeric($frequencyDelta) ? h(((float)$frequencyDelta > 0 ? '+' : '') . formatTruncated($frequencyDelta) . ' Hz') : '--'; ?></span>
                    </div>
                    <?php $netTransfer = $snapshot['net_interconnector_mw'] ?? null; $isImporting = is_numeric($netTransfer) && (float)$netTransfer >= 0; ?>
                    <span class="net-transfer-badge <?php echo is_numeric($netTransfer) ? ($isImporting ? 'importing' : 'exporting') : ''; ?>">
                        <span class="label">Net Transfer</span>
                        <span><?php echo is_numeric($netTransfer) ? ($isImporting ? 'Importing ' : 'Exporting ') . h(formatGw(abs((float)$netTransfer))) : '--'; ?></span>
                    </span>
                </div>
            </div>
        </div>
    </section>

    <!-- Detail Grid -->
    <section class="detail">
        <h2>Fuel & Transfer Breakdown (LIVE)</h2>
        <?php
        $detailCategories = [
            'Fossil Fuels' => [['Gas', ['gas_mw']], ['Coal', ['coal_mw']]],
            'Renewables' => [['Wind', ['wind_mw']], ['Solar', ['solar_mw']], ['Hydro', ['hydro_mw']]],
            'Other Sources' => [['Nuclear', ['nuclear_mw']], ['Biomass', ['biomass_mw']]],
            'Interconnectors' => [
                ['Belgium', ['intercon_nemo_mw']], ['Denmark', ['intercon_viking_mw']],
                ['France', ['intercon_ifa_mw', 'intercon_ifa2_mw', 'intercon_eleclink_mw']],
                ['Ireland', ['intercon_moyle_mw', 'intercon_ewic_mw', 'intercon_greenlink_mw']],
                ['Netherlands', ['intercon_britned_mw']], ['Norway', ['intercon_nsl_mw']],
            ],
            'Storage' => [['Pumped Storage', ['pumped_storage_mw']], ['Battery', ['battery_storage_mw']]],
        ];
        $demand = $snapshot['total_demand_mw'] ?? null;
        ?>
        <div class="detail-grid">
            <?php foreach ($detailCategories as $category => $items): ?>
            <article class="detail-card">
                <h3><?php echo h($category); ?></h3>
                <?php foreach ($items as $item): $value = sumValues($snapshot, $item[1]); ?>
                <div class="detail-row">
                    <span class="name"><?php echo h($item[0]); ?></span>
                    <span class="val"><?php echo h(formatGw($value)); ?></span>
                    <span class="pct"><?php echo is_numeric($value) && is_numeric($demand) && (float)$demand !== 0.0 ? h(number_format((float)$value / (float)$demand * 100, 1, '.', '') . '%') : '--'; ?></span>
                </div>
                <?php endforeach; ?>
            </article>
            <?php endforeach; ?>
        </div>
    </section>

    <!-- Regional Section -->
    <section class="regional">
        <h2>Regional Carbon Intensity - 14 DNO Regions (LIVE)</h2>
        <div class="legend-swatches">
            <span><i style="background:var(--very-low)"></i>very low</span>
            <span><i style="background:var(--low)"></i>low</span>
            <span><i style="background:var(--moderate)"></i>moderate</span>
            <span><i style="background:var(--high)"></i>high</span>
            <span><i style="background:var(--very-high)"></i>very high</span>
        </div>
        <div class="region-grid">
            <?php foreach ($regions['rows'] as $region): $level = $region['index_level'] ?? ''; ?>
            <article class="region-card" style="border-left-color:<?php echo h(regionColor($level)); ?>">
                <div class="name"><?php echo h($region['dno_region'] ?? 'Unknown region'); ?></div>
                <div class="intensity" style="color:<?php echo h(regionColor($level)); ?>"><?php echo h(rawValue($region['carbon_intensity'] ?? null)); ?><span class="unit"> gCO₂/kWh</span></div>
                <div class="meta"><span><?php echo h($level ?: '--'); ?></span><span><?php echo h($region['top_fuel'] ?? '--'); ?></span></div>
            </article>
            <?php endforeach; ?>
            <?php if (!$regions['rows']): ?><p class="chart-empty">Regional readings are currently unavailable.</p><?php endif; ?>
        </div>
    </section>
    <?php elseif ($page === 'fuelmix'): ?>
    <?php
    $historyByRegion = [];
    foreach ($regionalHistory['rows'] as $historyRow) {
        $regionId = $historyRow['region_id'] ?? 'unknown';
        $historyByRegion[$regionId]['name'] = $historyRow['dno_region'] ?? ('Region ' . $regionId);
        $historyByRegion[$regionId]['rows'][] = $historyRow;
    }
    ksort($historyByRegion);
    ?>
    <section class="regional">
        <nav class="region-history-nav" aria-label="Regional history day">
            <a class="<?php echo $regionalPeriod === 'day' ? 'active' : ''; ?>" href="<?php echo h(dashboardUrl($page, $period, 'day')); ?>"<?php echo $regionalPeriod === 'day' ? ' aria-current="page"' : ''; ?>>Last 24 hours</a>
            <a class="<?php echo $regionalPeriod === 'previous_day' ? 'active' : ''; ?>" href="<?php echo h(dashboardUrl($page, $period, 'previous_day')); ?>"<?php echo $regionalPeriod === 'previous_day' ? ' aria-current="page"' : ''; ?>>Previous Day</a>
        </nav>
        <?php $regionalPeriodLabel = $regionalPeriod === 'previous_day' ? 'Previous Day' : 'Last 24h'; ?>
        <h2>Regional Carbon Intensity (gCO₂/kWh) - <?php echo h($regionalPeriodLabel); ?></h2>
        <div class="legend-swatches">
            <span><i style="background:var(--very-low)"></i>very low</span>
            <span><i style="background:var(--low)"></i>low</span>
            <span><i style="background:var(--moderate)"></i>moderate</span>
            <span><i style="background:var(--high)"></i>high</span>
            <span><i style="background:var(--very-high)"></i>very high</span>
        </div>
        <div class="region-history">
            <?php foreach ($historyByRegion as $region): ?>
            <div class="region-history-row">
                <span class="name"><?php echo h($region['name']); ?></span>
                <div class="region-history-cells">
                    <?php foreach ($region['rows'] as $periodRow):
                        $historyLevel = $periodRow['index_level'] ?? '';
                        $historyTitle = formatGridTime($periodRow['timestamp'] ?? null) . ' - ' . ($periodRow['top_fuel'] ?? '--') . ', ' . rawValue($periodRow['carbon_intensity'] ?? null) . ' gCO₂/kWh (' . ($historyLevel ?: '--') . ')';
                    ?>
                    <span class="cell" title="<?php echo h($historyTitle); ?>" style="background:<?php echo h(regionColor($historyLevel)); ?>"></span>
                    <?php endforeach; ?>
                </div>
            </div>
            <?php endforeach; ?>
            <?php if (!$historyByRegion): ?><p class="chart-empty"><?php echo $regionalPeriod === 'previous_day' ? 'Previous-day regional history is currently unavailable.' : 'Regional history is currently unavailable.'; ?></p><?php endif; ?>
        </div>

        <h2 class="region-fuel-heading">Primary Fuel Source by DNO Region - <?php echo h($regionalPeriodLabel); ?></h2>
        <div class="legend-swatches region-fuel-legend">
            <?php foreach ([
                'Biomass' => 'biomass',
                'Coal' => 'coal',
                'Gas' => 'gas',
                'Hydro' => 'hydro',
                'Imports' => 'imports',
                'Nuclear' => 'nuclear',
                'Solar' => 'solar',
                'Wind' => 'wind',
            ] as $fuelLabel => $fuelKey): ?>
            <span><i style="background:<?php echo h(fuelColor($fuelKey)); ?>"></i><?php echo h($fuelLabel); ?></span>
            <?php endforeach; ?>
        </div>
        <div class="region-history">
            <?php foreach ($historyByRegion as $region): ?>
            <div class="region-history-row">
                <span class="name"><?php echo h($region['name']); ?></span>
                <div class="region-history-cells">
                    <?php foreach ($region['rows'] as $periodRow):
                        $primaryFuel = (string)($periodRow['top_fuel'] ?? '');
                        $fuelTitle = formatGridTime($periodRow['timestamp'] ?? null) . ' - Primary fuel: ' . ($primaryFuel !== '' ? $primaryFuel : '--') . ', ' . rawValue($periodRow['carbon_intensity'] ?? null) . ' gCO₂/kWh';
                    ?>
                    <span class="cell" title="<?php echo h($fuelTitle); ?>" style="background:<?php echo h(fuelColor($primaryFuel)); ?>"></span>
                    <?php endforeach; ?>
                </div>
            </div>
            <?php endforeach; ?>
            <?php if (!$historyByRegion): ?><p class="chart-empty"><?php echo $regionalPeriod === 'previous_day' ? 'Previous-day regional history is currently unavailable.' : 'Regional history is currently unavailable.'; ?></p><?php endif; ?>
        </div>
    </section>
    <?php endif; ?>

</div> <!-- End #app -->

<!-- Footer -->
<footer>
    <nav class="footer-links">
        <a href="terms.html">Terms of Service</a>
        <span aria-hidden="true">&middot;</span>
        <a href="privacy.html">Privacy Policy</a>
        <span aria-hidden="true">&middot;</span>
        <a href="https://ukpoliticsdecoded.uk/" target="_blank" rel="noopener">UK Politics Decoded</a>
        <span aria-hidden="true">&middot;</span>
        <a href="https://github.com/UKPoliticsDecoded/UK-Grid-Tracker" target="_blank" rel="noopener">GitHub data repo</a>
    </nav>
    <br>
    <p>Data refreshed from static exports in the project data repository. Values shown are unrounded source readings.</p>
    <p>
      The data comes from the <a href="https://bmrs.elexon.co.uk/" target="_blank" rel="noopener noreferrer">Elexon Insights Solution</a>,
      the <a href="https://www.neso.energy/data-portal" target="_blank" rel="noopener noreferrer">National Energy System Operator Data Portal</a>,
      and the <a href="https://carbonintensity.org.uk/" target="_blank" rel="noopener noreferrer">Carbon Intensity API</a>
      (a project by the National Energy System Operator and the University Of Oxford Department Of Computer Science).
      <a href="https://www.elexon.co.uk/data/balancing-mechanism-reporting-agent/copyright-licence-bmrs-data/" target="_blank" rel="noopener noreferrer">Elexon&rsquo;s licence</a>
      requires the following statement: Contains BMRS data &copy; Elexon Limited copyright and database right 2026.
    </p>
    <br>
    <p>Copyright 2026 UK Politics Decoded. All rights reserved.</p>
    <p>This website is not affiliated with the UK government or any other official body.</p>
</footer>

<script>
(() => {
    'use strict';

    const storageKey = 'regional-history-scroll';
    history.scrollRestoration = 'manual';

    const savedPosition = sessionStorage.getItem(storageKey);
    if (savedPosition) {
        const saved = JSON.parse(savedPosition);
        if (saved.path === location.pathname + location.search) {
            sessionStorage.removeItem(storageKey);
            requestAnimationFrame(() => {
                requestAnimationFrame(() => window.scrollTo(0, saved.y));
            });
        }
    }

    document.querySelectorAll('.region-history-nav a').forEach(link => {
        link.addEventListener('click', () => {
            const destination = new URL(link.href, location.href);
            sessionStorage.setItem(storageKey, JSON.stringify({
                path: destination.pathname + destination.search,
                y: window.scrollY
            }));
        });
    });
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js"></script>
<script>
(() => {
    'use strict';

    if (typeof uPlot === 'undefined') {
        console.error('Interactive charts could not load because uPlot is unavailable.');
        return;
    }

    const timeFormatter = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'Europe/London',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    });
    const axisColor = getComputedStyle(document.documentElement)
        .getPropertyValue('--text-dim').trim();
    const gridColor = getComputedStyle(document.documentElement)
        .getPropertyValue('--border').trim();

    document.querySelectorAll('.server-chart').forEach(figure => {
        const dataElement = figure.querySelector('.interactive-chart-data');
        const host = figure.querySelector('.interactive-chart-host');
        const fallback = figure.querySelector('.chart-svg');
        if (!dataElement || !host || !fallback) return;

        try {
            const config = JSON.parse(dataElement.textContent);
            const hasRightAxis = config.series.some(item => item.axis === 'right');
            const axes = [
                {
                    scale: 'x',
                    stroke: axisColor,
                    grid: { stroke: gridColor },
                    values: (chart, values) => values.map(value =>
                        timeFormatter.format(new Date(value * 1000))
                    )
                },
                { scale: 'left', label: '', stroke: axisColor, grid: { stroke: gridColor } }
            ];
            const scales = { x: { time: true }, left: { auto: true } };
            if (hasRightAxis) {
                axes.push({ scale: 'right', side: 1, label: '', stroke: axisColor, grid: { show: false } });
                scales.right = { auto: true };
            }

            const plot = new uPlot({
                width: Math.max(host.clientWidth, 320),
                height: config.height,
                scales,
                axes,
                cursor: { drag: { setScale: true } },
                series: [
                    { label: 'Time' },
                    ...config.series.map(item => ({
                        label: item.label,
                        scale: item.axis === 'right' ? 'right' : 'left',
                        stroke: item.color,
                        width: 1.5,
                        points: { show: false },
                        value: (chart, value) => value == null ? '--' : String(value)
                    }))
                ],
                hooks: {
                    ready: [chart => {
                        chart.root.addEventListener('dblclick', () => {
                            chart.setScale('x', { min: null, max: null });
                        });
                    }]
                }
            }, [
                config.data.timestamps,
                ...config.data.values
            ], host);

            fallback.hidden = true;
            fallback.style.display = 'none';
            const staticLegend = figure.querySelector('.chart-legend');
            staticLegend.hidden = true;
            staticLegend.style.display = 'none';
            figure.classList.add('interactive');

            new ResizeObserver(() => {
                if (host.clientWidth > 0) {
                    plot.setSize({ width: host.clientWidth, height: config.height });
                }
            }).observe(host);
        } catch (error) {
            console.error('Failed to initialize dashboard chart.', error);
        }
    });
})();
</script>
</body>
</html>