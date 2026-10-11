<?php
declare(strict_types=1);
require __DIR__ . '/dotmac-fiber-outbox.php';
function check(bool $ok, string $name): void { if (!$ok) throw new RuntimeException($name); }
function fixtureReceipt(string $reference = 'FBR-FIXTURE', string $status = 'manual_review'): DotmacFiberReceipt {
    return new DotmacFiberReceipt(['reference' => $reference, 'coverage' => ['status' => $status, 'summary' => 'Synthetic fixture result.']]);
}
$directory = sys_get_temp_dir() . '/fiber-outbox-fixture-' . bin2hex(random_bytes(8));
mkdir($directory, 0700);
$key = random_bytes(32);
$outbox = new DotmacFiberOutbox($directory, $key);
$id = hash('sha256', 'fixture');
$fingerprint = hash('sha256', 'immutable input');
$body = '{"submitted_at":"2026-10-11T12:00:00Z","address":"PRIVATE-FIXTURE","attribution":{"journey_id":"fixed"}}';
$factory = static fn(): DotmacFiberEnvelope => new DotmacFiberEnvelope($fingerprint, $body, 'fiber-' . $id, 'https://receiver.example.test');
$calls = 0;
$committed = [];
$send = static function (DotmacFiberEnvelope $envelope) use (&$calls, &$committed, $body): DotmacFiberDeliveryResult {
    $calls++;
    check($body === $envelope->body, 'retry changed bytes');
    $committed[$envelope->deliveryId] = $committed[$envelope->deliveryId] ?? fixtureReceipt();
    return $calls === 1 ? new DotmacFiberDeliveryResult('pending') : new DotmacFiberDeliveryResult('received', $committed[$envelope->deliveryId]);
};
$first = $outbox->deliver($id, $fingerprint, $factory, $send, 1000);
check($first->state === 'pending' && count($committed) === 1, 'lost response is pending');
check(!str_contains(file_get_contents($directory . '/' . $id . '.json'), 'PRIVATE-FIXTURE'), 'plaintext payload stored');
$restarted = new DotmacFiberOutbox($directory, $key);
$neverRebuild = static function (): DotmacFiberEnvelope { throw new RuntimeException('envelope rebuilt on retry'); };
$retry = $restarted->deliver($id, $fingerprint, $neverRebuild, $send, 1061);
check($retry->state === 'received' && $retry->receipt->reference === 'FBR-FIXTURE' && count($committed) === 1, 'restart recovery/replay');
$cached = $restarted->deliver($id, $fingerprint, $neverRebuild, $send, 2000);
check($calls === 2 && $cached->receipt->reference === $retry->receipt->reference, 'durable confirmed receipt');
try { $restarted->deliver($id, hash('sha256', 'changed'), $neverRebuild, $send, 2000); throw new RuntimeException('changed content accepted'); }
catch (DotmacFiberDeliveryException $error) { check($error->getMessage() === 'submission_content_conflict', 'wrong conflict'); }
foreach (['covered', 'survey_required', 'out_of_area', 'manual_review', 'technical_error'] as $status) check(fixtureReceipt('FBR-' . $status, $status)->coverageStatus === $status, 'coverage state');
try { new DotmacFiberReceipt(['reference' => '', 'coverage' => ['status' => 'made_up', 'summary' => '']]); throw new RuntimeException('malformed receipt accepted'); }
catch (DotmacFiberDeliveryException $error) {}
$badId = hash('sha256', 'malformed');
$malformed = static function (DotmacFiberEnvelope $envelope): DotmacFiberDeliveryResult { return new DotmacFiberDeliveryResult('received', new DotmacFiberReceipt(['success' => true])); };
$result = $restarted->deliver($badId, $fingerprint, $factory, $malformed, 1000);
check($result->state === 'pending', 'malformed receipt confirmed');
$pending = static fn(DotmacFiberEnvelope $envelope): DotmacFiberDeliveryResult => new DotmacFiberDeliveryResult('pending');
for ($attempt = 1; $attempt <= 12; $attempt++) $result = $restarted->deliver($badId, $fingerprint, $neverRebuild, $pending, 1000 + $attempt * 21601);
check($result->state === 'attention', 'retry bound missing');
$restarted->recover($badId, 300000);
check($restarted->deliver($badId, $fingerprint, $neverRebuild, $send, 300001)->state === 'received', 'operator recovery');
$expiredId = hash('sha256', 'expired');
$restarted->deliver($expiredId, $fingerprint, $factory, $pending, 1000);
check($restarted->deliver($expiredId, $fingerprint, $neverRebuild, $pending, 1000 + 7 * 86400)->state === 'expired', 'expiry not visible');
check($restarted->deliver(hash('sha256', 'unknown'), '', $neverRebuild, $pending, 1000)->state === 'unknown', 'unknown status');
// Real multiprocess flock test; fixture count is transport evidence, not Selfcare Lead evidence.
if (!function_exists('pcntl_fork')) throw new RuntimeException('pcntl required for concurrency evidence');
$concurrentId = hash('sha256', 'concurrent');
$countFile = $directory . '/fixture-count';
file_put_contents($countFile, '0');
$children = [];
for ($i = 0; $i < 6; $i++) {
    $pid = pcntl_fork();
    if ($pid === -1) throw new RuntimeException('fork failed');
    if ($pid === 0) {
        try {
            (new DotmacFiberOutbox($directory, $key))->deliver($concurrentId, $fingerprint, $factory,
                static function (DotmacFiberEnvelope $envelope) use ($countFile): DotmacFiberDeliveryResult {
                    file_put_contents($countFile, (string)((int)file_get_contents($countFile) + 1));
                    usleep(200000);
                    return new DotmacFiberDeliveryResult('received', fixtureReceipt());
                }, 1000);
            exit(0);
        } catch (Throwable $error) { exit(1); }
    }
    $children[] = $pid;
}
foreach ($children as $pid) { pcntl_waitpid($pid, $status); check(pcntl_wexitstatus($status) === 0, 'child failed'); }
check(file_get_contents($countFile) === '1', 'concurrent double delivery');
$restartId = hash('sha256', 'process-died-after-commit');
$pid = pcntl_fork();
if ($pid === 0) {
    (new DotmacFiberOutbox($directory, $key))->deliver($restartId, $fingerprint, $factory,
        static function (DotmacFiberEnvelope $envelope): DotmacFiberDeliveryResult { exit(0); }, 1000);
    exit(1);
}
pcntl_waitpid($pid, $status);
check(pcntl_wexitstatus($status) === 0, 'crash fixture failed');
$afterDeath = (new DotmacFiberOutbox($directory, $key))->deliver($restartId, $fingerprint, $neverRebuild,
    static function (DotmacFiberEnvelope $envelope) use ($body): DotmacFiberDeliveryResult {
        check($envelope->body === $body, 'process restart changed bytes');
        return new DotmacFiberDeliveryResult('received', fixtureReceipt());
    }, 1061);
check($afterDeath->state === 'received', 'process restart failed');
$lastId = str_repeat('f', 64);
$restarted->deliver($lastId, $fingerprint, $factory, $pending, 1000);
for ($page = 0; $page < 10; $page++) $restarted->run(
    static fn(DotmacFiberEnvelope $envelope): DotmacFiberDeliveryResult => new DotmacFiberDeliveryResult('received', fixtureReceipt()), 2000, 2);
check($restarted->deliver($lastId, $fingerprint, $neverRebuild, $pending, 2000)->state === 'received', 'worker starved later record');
check(is_file($directory . '/worker.cursor'), 'worker cursor missing');
foreach (new DirectoryIterator($directory) as $file) if (!$file->isDot()) unlink($file->getPathname());
rmdir($directory);
echo "PASS: encrypted persistence, frozen retries, lost response, restart, conflict, receipt validation, coverage states, retry bound, operator recovery, expiry, concurrent claim, worker cursor\n";
