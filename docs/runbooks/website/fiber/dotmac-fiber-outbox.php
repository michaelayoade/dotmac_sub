<?php
/** Durable website transport. Selfcare alone owns saved Leads and coverage. */
declare(strict_types=1);

final class DotmacFiberDeliveryException extends RuntimeException {}

final class DotmacFiberReceipt {
    public string $reference;
    public string $coverageStatus;
    public string $summary;
    public function __construct(array $wire) {
        $coverage = $wire['coverage'] ?? null;
        if (!is_string($wire['reference'] ?? null) || trim($wire['reference']) === '' || strlen($wire['reference']) > 160
            || !is_array($coverage) || !in_array($coverage['status'] ?? null,
                ['covered', 'survey_required', 'out_of_area', 'manual_review', 'technical_error'], true)
            || !is_string($coverage['summary'] ?? null) || trim($coverage['summary']) === '' || strlen($coverage['summary']) > 4000) {
            throw new DotmacFiberDeliveryException('invalid_receipt');
        }
        $this->reference = $wire['reference'];
        $this->coverageStatus = $coverage['status'];
        $this->summary = $coverage['summary'];
    }
    public function publicResponse(): array {
        return ['enquiry_status' => 'received', 'reference' => $this->reference,
            'coverage' => ['status' => $this->coverageStatus, 'summary' => $this->summary]];
    }
}

final class DotmacFiberEnvelope {
    public string $fingerprint;
    public string $body;
    public string $deliveryId;
    public string $url;
    public function __construct(string $fingerprint, string $body, string $deliveryId, string $url) {
        $this->fingerprint = $fingerprint;
        $this->body = $body;
        $this->deliveryId = $deliveryId;
        $this->url = $url;
    }
}

final class DotmacFiberDeliveryResult {
    public string $state;
    public ?DotmacFiberReceipt $receipt;
    public function __construct(string $state, ?DotmacFiberReceipt $receipt = null) {
        if ($state === 'received' && !$receipt) throw new DotmacFiberDeliveryException('receipt_required');
        $this->state = $state;
        $this->receipt = $receipt;
    }
}

/** One local filesystem, one writer under flock, encrypted atomic replacements.
 * Never use this store on NFS or multiple hosts. Provision the private directory
 * explicitly; loading the MU plugin does not create it or enable a worker.
 */
final class DotmacFiberOutbox {
    private string $directory;
    private string $key;
    private const MAX_ATTEMPTS = 12;
    private const MAX_AGE = 7 * 86400;

    public function __construct(string $directory, string $key) {
        $resolved = realpath($directory);
        if (!$resolved || is_link($directory) || !is_dir($resolved) || !is_writable($resolved)
            || (fileperms($resolved) & 0077) !== 0 || strlen($key) !== 32) {
            throw new DotmacFiberDeliveryException('outbox_storage_unavailable');
        }
        foreach (array_filter([defined('ABSPATH') ? ABSPATH : null, $_SERVER['DOCUMENT_ROOT'] ?? null]) as $root) {
            $root = realpath($root);
            if ($root && ($resolved === $root || str_starts_with($resolved, rtrim($root, '/') . '/'))) {
                throw new DotmacFiberDeliveryException('outbox_storage_public');
            }
        }
        $this->directory = $resolved;
        $this->key = $key;
    }

    private function path(string $id): string {
        if (!preg_match('/^[a-f0-9]{64}$/D', $id)) {
            throw new DotmacFiberDeliveryException('invalid_delivery_identity');
        }
        return $this->directory . '/' . $id;
    }

    private function read(string $path): ?array {
        if (!file_exists($path . '.json')) return null;
        if (is_link($path . '.json')) throw new DotmacFiberDeliveryException('unsafe_storage');
        $encoded = file_get_contents($path . '.json');
        $sealed = $encoded === false ? false : base64_decode($encoded, true);
        if ($sealed === false || strlen($sealed) < 28) throw new DotmacFiberDeliveryException('corrupt_storage');
        $plain = openssl_decrypt(substr($sealed, 28), 'aes-256-gcm', $this->key, OPENSSL_RAW_DATA,
            substr($sealed, 0, 12), substr($sealed, 12, 16), basename($path));
        if ($plain === false) throw new DotmacFiberDeliveryException('unreadable_storage');
        $record = json_decode($plain, true, 32, JSON_THROW_ON_ERROR);
        if (!is_array($record) || ($record['version'] ?? null) !== 1) throw new DotmacFiberDeliveryException('invalid_storage_version');
        return $record;
    }

    private function write(string $path, array $record): void {
        $iv = random_bytes(12);
        $tag = '';
        $cipher = openssl_encrypt(json_encode($record, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
            'aes-256-gcm', $this->key, OPENSSL_RAW_DATA, $iv, $tag, basename($path));
        if ($cipher === false) throw new DotmacFiberDeliveryException('storage_encryption_failed');
        $temp = $path . '.' . bin2hex(random_bytes(8)) . '.tmp';
        $handle = fopen($temp, 'x+b');
        if (!$handle) throw new DotmacFiberDeliveryException('storage_write_failed');
        try {
            if (!chmod($temp, 0600)) throw new DotmacFiberDeliveryException('storage_permissions_failed');
            $bytes = base64_encode($iv . $tag . $cipher);
            $offset = 0;
            while ($offset < strlen($bytes)) {
                $written = fwrite($handle, substr($bytes, $offset));
                if (!$written) throw new DotmacFiberDeliveryException('storage_write_failed');
                $offset += $written;
            }
            if (!fflush($handle) || !fsync($handle)) throw new DotmacFiberDeliveryException('storage_sync_failed');
            fclose($handle);
            $handle = null;
            if (!rename($temp, $path . '.json')) throw new DotmacFiberDeliveryException('storage_replace_failed');
            $directory = fopen($this->directory, 'r');
            if (!$directory || !fsync($directory)) throw new DotmacFiberDeliveryException('directory_sync_failed');
            fclose($directory);
        } finally {
            if (is_resource($handle)) fclose($handle);
            if (file_exists($temp)) unlink($temp);
        }
    }

    /** The lock is retained during HTTP. A crashed process releases it; attempts
     * and next retry were durably reserved before HTTP, so crash loops are bounded.
     * Envelope factory is called only for a new ID, never for an existing retry.
     */
    public function deliver(string $id, string $fingerprint, callable $factory, callable $send, int $now): DotmacFiberDeliveryResult {
        $path = $this->path($id);
        if ($fingerprint === '' && !file_exists($path . '.json')) return new DotmacFiberDeliveryResult('unknown');
        if (is_link($path . '.lock')) throw new DotmacFiberDeliveryException('unsafe_storage');
        $lock = fopen($path . '.lock', 'c+b');
        if (!$lock || !chmod($path . '.lock', 0600)) throw new DotmacFiberDeliveryException('storage_lock_failed');
        if (!flock($lock, LOCK_EX | LOCK_NB)) {
            $existing = $this->read($path);
            if ($existing && $fingerprint !== '' && !hash_equals($existing['fingerprint'], $fingerprint)) {
                fclose($lock);
                throw new DotmacFiberDeliveryException('submission_content_conflict');
            }
            fclose($lock);
            return new DotmacFiberDeliveryResult('pending');
        }
        try {
            $record = $this->read($path);
            if ($record && $fingerprint !== '' && !hash_equals($record['fingerprint'], $fingerprint)) {
                throw new DotmacFiberDeliveryException('submission_content_conflict');
            }
            if (!$record) {
                if ($fingerprint === '') return new DotmacFiberDeliveryResult('unknown');
                if (count(scandir($this->directory)) >= 20000) throw new DotmacFiberDeliveryException('outbox_capacity_exceeded');
                $envelope = $factory();
                if (!$envelope instanceof DotmacFiberEnvelope || !hash_equals($fingerprint, $envelope->fingerprint)) {
                    throw new DotmacFiberDeliveryException('invalid_envelope');
                }
                $record = ['version' => 1, 'fingerprint' => $fingerprint, 'body' => $envelope->body,
                    'delivery_id' => $envelope->deliveryId, 'url' => $envelope->url, 'created' => $now,
                    'attempts' => 0, 'next' => $now, 'state' => 'pending', 'receipt' => null, 'reason' => null];
                $this->write($path, $record);
            }
            if ($record['state'] === 'received') {
                // Retain receipt + fingerprint tombstone; remove customer payload after 30 days.
                if ($record['body'] !== null && $now - $record['created'] >= 30 * 86400) {
                    $record['body'] = null;
                    $this->write($path, $record);
                }
                return new DotmacFiberDeliveryResult('received', new DotmacFiberReceipt($record['receipt']));
            }
            if ($now - $record['created'] >= self::MAX_AGE) {
                $record['body'] = null;
                $record['state'] = 'expired';
                $record['reason'] = 'confirmation_deadline_expired';
                $this->write($path, $record);
            }
            if ($record['state'] !== 'pending' || $record['next'] > $now) return new DotmacFiberDeliveryResult($record['state']);
            if ($record['attempts'] >= self::MAX_ATTEMPTS) {
                $record['state'] = 'attention';
                $record['reason'] = 'attempts_exhausted';
                $this->write($path, $record);
                return new DotmacFiberDeliveryResult('attention');
            }
            $record['attempts']++;
            $record['next'] = $now + min(21600, 60 * (2 ** ($record['attempts'] - 1)));
            $this->write($path, $record);
            $envelope = new DotmacFiberEnvelope($record['fingerprint'], $record['body'], $record['delivery_id'], $record['url']);
            try {
                $result = $send($envelope);
                if (!$result instanceof DotmacFiberDeliveryResult || !in_array($result->state, ['received', 'pending', 'attention'], true)) {
                    throw new DotmacFiberDeliveryException('invalid_transport_result');
                }
                $record['state'] = $result->state;
                $record['receipt'] = $result->receipt ? $result->receipt->publicResponse() : null;
                $record['reason'] = $result->state === 'received' ? null : 'confirmation_unavailable';
            } catch (Throwable $error) {
                // Never log provider exception text or payload: it may contain PII/secrets.
                $record['reason'] = 'transport_exception';
            }
            if ($record['state'] === 'pending' && $record['attempts'] >= self::MAX_ATTEMPTS) $record['state'] = 'attention';
            $this->write($path, $record);
            return new DotmacFiberDeliveryResult($record['state'], $record['receipt'] ? new DotmacFiberReceipt($record['receipt']) : null);
        } finally {
            flock($lock, LOCK_UN);
            fclose($lock);
        }
    }

    public function run(callable $send, int $now, int $limit = 50): array {
        $states = [];
        $count = 0;
        $cursor = fopen($this->directory . '/worker.cursor', 'c+b');
        if (!$cursor || !chmod($this->directory . '/worker.cursor', 0600)) throw new DotmacFiberDeliveryException('cursor_unavailable');
        if (!flock($cursor, LOCK_EX | LOCK_NB)) { fclose($cursor); return ['worker_busy' => 1]; }
        try {
            $last = trim(stream_get_contents($cursor));
            $files = array_values(array_filter(scandir($this->directory), static fn(string $name): bool => (bool)preg_match('/^[a-f0-9]{64}\.json$/D', $name)));
            $remaining = array_values(array_filter($files, static fn(string $name): bool => $name > $last));
            if (!$remaining) $remaining = $files;
            foreach ($remaining as $name) {
                if (++$count > max(1, min(50, $limit))) break;
                try {
                    $result = $this->deliver(substr($name, 0, 64), '', static function () { throw new DotmacFiberDeliveryException('missing_envelope'); }, $send, $now);
                    $states[$result->state] = ($states[$result->state] ?? 0) + 1;
                } catch (Throwable $error) { $states['storage_error'] = ($states['storage_error'] ?? 0) + 1; }
                rewind($cursor);
                ftruncate($cursor, 0);
                fwrite($cursor, $name);
                fflush($cursor);
            }
        } finally {
            flock($cursor, LOCK_UN);
            fclose($cursor);
        }
        return $states;
    }

    public function inventory(): array {
        $rows = [];
        foreach (new DirectoryIterator($this->directory) as $file) {
            if (!preg_match('/^([a-f0-9]{64})\.json$/D', $file->getFilename(), $match)) continue;
            $record = $this->read($this->path($match[1]));
            $rows[] = ['id' => $match[1], 'state' => $record['state'], 'attempts' => $record['attempts'],
                'created' => $record['created'], 'next' => $record['next'], 'reason' => $record['reason']];
        }
        return $rows;
    }

    public function recover(string $id, int $now): void {
        $path = $this->path($id);
        $lock = fopen($path . '.lock', 'c+b');
        if (!$lock || !flock($lock, LOCK_EX | LOCK_NB)) throw new DotmacFiberDeliveryException('delivery_busy');
        try {
            $record = $this->read($path);
            if (!$record || $record['state'] !== 'attention' || !$record['body']
                || $now - $record['created'] >= self::MAX_AGE || ($record['recoveries'] ?? 0) >= 1) {
                throw new DotmacFiberDeliveryException('recovery_refused');
            }
            $record['recoveries'] = 1;
            $record['previous_attempts'] = $record['attempts'];
            $record['attempts'] = 0;
            $record['state'] = 'pending';
            $record['next'] = $now;
            $record['reason'] = 'operator_recovery';
            $this->write($path, $record);
        } finally { flock($lock, LOCK_UN); fclose($lock); }
    }
}
