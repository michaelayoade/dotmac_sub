<?php
/**
 * Plugin Name: Dotmac Fiber Acquisition
 * Description: First-party campaign attribution and signed Selfcare quote relay.
 * Version: 1.2.0
 */

if (!defined('ABSPATH')) {
    exit;
}

require_once __DIR__ . '/dotmac-fiber-outbox.php';

const DOTMAC_FIBER_COOKIE = 'dotmac_fiber_attribution';
const DOTMAC_FIBER_COOKIE_DAYS = 30;

function dotmac_fiber_clean($value, $length = 200) {
    return mb_substr(sanitize_text_field((string) $value), 0, $length);
}

function dotmac_fiber_base64url_encode($value) {
    return rtrim(strtr(base64_encode($value), '+/', '-_'), '=');
}

function dotmac_fiber_base64url_decode($value) {
    return base64_decode(strtr($value, '-_', '+/'));
}

function dotmac_fiber_cookie_secret() {
    return hash('sha256', wp_salt('auth') . '|dotmac-fiber-attribution');
}

function dotmac_fiber_read_attribution() {
    $raw = isset($_COOKIE[DOTMAC_FIBER_COOKIE]) ? (string) $_COOKIE[DOTMAC_FIBER_COOKIE] : '';
    $parts = explode('.', $raw, 2);
    if (count($parts) !== 2) {
        return null;
    }
    $json = dotmac_fiber_base64url_decode($parts[0]);
    $expected = hash_hmac('sha256', $json, dotmac_fiber_cookie_secret());
    if (!hash_equals($expected, $parts[1])) {
        return null;
    }
    $data = json_decode($json, true);
    if (!is_array($data) || empty($data['journey_id']) || empty($data['expires_at'])) {
        return null;
    }
    if ((int) $data['expires_at'] < time()) {
        return null;
    }
    return $data;
}

function dotmac_fiber_write_attribution($data) {
    $json = wp_json_encode($data, JSON_UNESCAPED_SLASHES);
    $value = dotmac_fiber_base64url_encode($json) . '.' . hash_hmac('sha256', $json, dotmac_fiber_cookie_secret());
    setcookie(DOTMAC_FIBER_COOKIE, $value, array(
        'expires' => (int) $data['expires_at'],
        'path' => '/',
        'secure' => is_ssl(),
        'httponly' => true,
        'samesite' => 'Lax',
    ));
    $_COOKIE[DOTMAC_FIBER_COOKIE] = $value;
}

function dotmac_fiber_is_direct($data) {
    $source = strtolower((string) ($data['utm_source'] ?? 'direct'));
    return $source === '' || $source === 'direct' || $source === '(direct)';
}

function dotmac_fiber_capture_attribution(WP_REST_Request $request) {
    $input = (array) $request->get_json_params();
    $candidate = array(
        'journey_id' => wp_generate_uuid4(),
        'utm_source' => dotmac_fiber_clean($input['utm_source'] ?? 'direct'),
        'utm_medium' => dotmac_fiber_clean($input['utm_medium'] ?? '(none)'),
        'utm_campaign' => dotmac_fiber_clean($input['utm_campaign'] ?? ''),
        'utm_content' => dotmac_fiber_clean($input['utm_content'] ?? ''),
        'utm_term' => dotmac_fiber_clean($input['utm_term'] ?? ''),
        'campaign_id' => dotmac_fiber_clean($input['campaign_id'] ?? ''),
        'ad_set_id' => dotmac_fiber_clean($input['ad_set_id'] ?? ''),
        'ad_id' => dotmac_fiber_clean($input['ad_id'] ?? ''),
        'click_id' => dotmac_fiber_clean($input['click_id'] ?? '', 255),
        'landing_path' => substr((string) (wp_parse_url(sanitize_text_field($input['landing_path'] ?? '/coverage/'), PHP_URL_PATH) ?: '/coverage/'), 0, 500),
        'captured_at' => gmdate('c'),
        'expires_at' => time() + (DAY_IN_SECONDS * DOTMAC_FIBER_COOKIE_DAYS),
    );
    $current = dotmac_fiber_read_attribution();
    if (!$current || (dotmac_fiber_is_direct($current) && !dotmac_fiber_is_direct($candidate))) {
        dotmac_fiber_write_attribution($candidate);
        $current = $candidate;
    }
    return new WP_REST_Response(array(
        'journey_id' => $current['journey_id'],
        'source' => $current['utm_source'],
        'expires_at' => gmdate('c', (int) $current['expires_at']),
    ), 200);
}

function dotmac_fiber_rate_limit() {
    $address = isset($_SERVER['REMOTE_ADDR']) ? (string) $_SERVER['REMOTE_ADDR'] : 'unknown';
    $key = 'dotmac_fiber_rate_' . hash_hmac('sha256', $address, wp_salt('nonce'));
    $count = (int) get_transient($key);
    if ($count >= 5) {
        return false;
    }
    set_transient($key, $count + 1, 10 * MINUTE_IN_SECONDS);
    return true;
}

function dotmac_fiber_upstream_config() {
    $required = array(
        'base_url' => 'DOTMAC_FIBER_SELFCARE_URL',
        'binding_id' => 'DOTMAC_FIBER_BINDING_ID',
        'secret' => 'DOTMAC_FIBER_SIGNING_SECRET',
    );
    $config = array();
    foreach ($required as $key => $constant) {
        if (!defined($constant) || !constant($constant)) {
            return null;
        }
        $config[$key] = (string) constant($constant);
    }
    $config['signature_header'] = defined('DOTMAC_FIBER_SIGNATURE_HEADER') ? constant('DOTMAC_FIBER_SIGNATURE_HEADER') : 'X-Dotmac-Fiber-Signature';
    $config['delivery_header'] = defined('DOTMAC_FIBER_DELIVERY_HEADER') ? constant('DOTMAC_FIBER_DELIVERY_HEADER') : 'X-Dotmac-Fiber-Delivery';
    $config['signature_prefix'] = defined('DOTMAC_FIBER_SIGNATURE_PREFIX') ? constant('DOTMAC_FIBER_SIGNATURE_PREFIX') : 'sha256=';
    return $config;
}

function dotmac_fiber_build_payload(WP_REST_Request $request): array|WP_Error {
    $input = (array) $request->get_json_params();
    foreach (array('full_name', 'phone', 'address') as $field) {
        if (empty($input[$field])) {
            return new WP_Error('fiber_missing_field', 'Please provide your installation address, name, and phone number.', array('status' => 422));
        }
    }
    if (!empty($input['email']) && !is_email($input['email'])) {
        return new WP_Error('fiber_invalid_email', 'Please enter a valid email address.', array('status' => 422));
    }
    $attribution = dotmac_fiber_read_attribution();
    if (!$attribution) {
        // Capture attribution in this request too: cookies can be missing or
        // the initial attribution request can still be in flight.
        $capture = new WP_REST_Request('POST');
        $capture->set_header('content-type', 'application/json');
        $capture->set_body(wp_json_encode(is_array($input['attribution'] ?? null) ? $input['attribution'] : array()));
        dotmac_fiber_capture_attribution($capture);
        $attribution = dotmac_fiber_read_attribution();
    }
    $attribution_payload = null;
    if ($attribution) {
        $attribution_payload = array_intersect_key($attribution, array_flip(array(
            'journey_id', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content',
            'utm_term', 'campaign_id', 'ad_set_id', 'ad_id', 'click_id', 'landing_path',
            'captured_at',
        )));
        $attribution_payload = array_filter($attribution_payload, static function ($value) {
            return $value !== '' && $value !== null;
        });
    }
    if (mb_strlen(dotmac_fiber_clean($input['full_name'], 200)) < 2 || mb_strlen(sanitize_textarea_field($input['address'])) < 5) {
        return new WP_Error('fiber_invalid_fields', 'Please enter your full name and complete installation address.', array('status' => 422));
    }
    if (!$attribution_payload) {
        return new WP_Error('fiber_attribution_unavailable', 'Please try again; campaign attribution could not be established.', array('status' => 503));
    }
    $location = array(
        'address' => substr(sanitize_textarea_field($input['address']), 0, 500),
        'area' => dotmac_fiber_clean($input['area'] ?? '', 80),
    );
    $has_latitude = isset($input['latitude']) && $input['latitude'] !== '';
    $has_longitude = isset($input['longitude']) && $input['longitude'] !== '';
    if ($has_latitude !== $has_longitude || ($has_latitude && (
        !is_numeric($input['latitude']) || !is_numeric($input['longitude']) ||
        (float) $input['latitude'] < -90 || (float) $input['latitude'] > 90 ||
        (float) $input['longitude'] < -180 || (float) $input['longitude'] > 180
    ))) {
        return new WP_Error('fiber_invalid_location', 'Please choose a valid installation location, or submit the address without a location pin.', array('status' => 422));
    }
    if ($has_latitude && $has_longitude) {
        $location['latitude'] = (string) $input['latitude'];
        $location['longitude'] = (string) $input['longitude'];
    }
    $payload = array(
        'form_version' => 'fiber-coverage-v1',
        'full_name' => dotmac_fiber_clean($input['full_name'], 200),
        'phone' => dotmac_fiber_clean($input['phone'], 40),
        'interest' => 'new_connection',
        'message' => substr(sanitize_textarea_field($input['message'] ?? 'Coverage and quotation request.'), 0, 4000),
        'submitted_at' => gmdate('c'),
        'location' => $location,
    );
    if (!empty($input['email'])) {
        $payload['email'] = sanitize_email($input['email']);
    }
    if (!empty($input['service'])) {
        // The v1 receiver has no top-level service field. Retain the customer's
        // preference in the enquiry text without changing the wire contract.
        $payload['message'] = mb_substr($payload['message'] . ' Service preference: ' . dotmac_fiber_clean($input['service'], 40) . '.', 0, 4000);
    }
    if (!empty($input['plan'])) {
        $payload['selected_plan'] = array('name' => dotmac_fiber_clean($input['plan'], 160));
    }
    if ($attribution_payload) {
        $payload['attribution'] = $attribution_payload;
    }
    return $payload;
}


final class DotmacFiberPayloadException extends RuntimeException {
    public WP_Error $error;
    public function __construct(WP_Error $error) { $this->error = $error; parent::__construct('invalid_submission'); }
}

function dotmac_fiber_outbox(): DotmacFiberOutbox {
    if (!defined('DOTMAC_FIBER_OUTBOX_DIR') || PHP_VERSION_ID < 80100 || !extension_loaded('openssl')) {
        throw new DotmacFiberDeliveryException('outbox_not_configured');
    }
    return new DotmacFiberOutbox(DOTMAC_FIBER_OUTBOX_DIR, hash('sha256', wp_salt('auth') . '|fiber-outbox-v1', true));
}

function dotmac_fiber_identity(array $input): string {
    $id = $input['submission_id'] ?? null;
    if (!is_string($id) || !preg_match('/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/iD', $id)) {
        throw new DotmacFiberDeliveryException('invalid_submission_identity');
    }
    return hash_hmac('sha256', strtolower($id), wp_salt('auth'));
}

function dotmac_fiber_canonical(array $value): array {
    ksort($value);
    foreach ($value as &$item) if (is_array($item)) $item = dotmac_fiber_canonical($item);
    return $value;
}

function dotmac_fiber_send_envelope(DotmacFiberEnvelope $envelope): DotmacFiberDeliveryResult {
    $config = dotmac_fiber_upstream_config();
    if (!$config) return new DotmacFiberDeliveryResult('attention');
    $url = rtrim($config['base_url'], '/') . '/api/v1/webhooks/fiber-inquiry/' . rawurlencode($config['binding_id']);
    if (!str_starts_with($url, 'https://') || !hash_equals($envelope->url, $url)) return new DotmacFiberDeliveryResult('attention');
    $response = wp_remote_post($url, [
        'timeout' => 20, 'redirection' => 0, 'limit_response_size' => 16384,
        'headers' => ['Content-Type' => 'application/json',
            $config['signature_header'] => $config['signature_prefix'] . hash_hmac('sha256', $envelope->body, $config['secret']),
            $config['delivery_header'] => $envelope->deliveryId], 'body' => $envelope->body,
    ]);
    if (is_wp_error($response)) return new DotmacFiberDeliveryResult('pending');
    $status = (int) wp_remote_retrieve_response_code($response);
    if ($status < 200 || $status >= 300) {
        return new DotmacFiberDeliveryResult(in_array($status, [408, 425, 429], true) || $status >= 500 ? 'pending' : 'attention');
    }
    try {
        $wire = json_decode(wp_remote_retrieve_body($response), true, 32, JSON_THROW_ON_ERROR);
        if (!is_array($wire)) throw new DotmacFiberDeliveryException('invalid_receipt');
        return new DotmacFiberDeliveryResult('received', new DotmacFiberReceipt($wire));
    } catch (Throwable $error) { return new DotmacFiberDeliveryResult('pending'); }
}

function dotmac_fiber_delivery_response(DotmacFiberDeliveryResult $result): WP_REST_Response|WP_Error {
    if ($result->state === 'received') return new WP_REST_Response($result->receipt->publicResponse(), 200);
    if ($result->state === 'unknown') return new WP_Error('fiber_submission_unknown', 'No stored submission was found. Retry the same pending request.', ['status' => 404]);
    return new WP_REST_Response(['enquiry_status' => 'pending_confirmation', 'delivery_state' => $result->state,
        'message' => $result->state === 'pending'
            ? 'Your request is pending confirmation. We will retry delivery; a saved Selfcare Lead has not yet been confirmed.'
            : 'We cannot confirm receipt yet. Please contact Dotmac for help with your pending request; do not send a new enquiry.'], 202);
}

function dotmac_fiber_quote_request(WP_REST_Request $request): WP_REST_Response|WP_Error {
    if (!dotmac_fiber_rate_limit()) return new WP_Error('fiber_rate_limited', 'Please wait a few minutes before trying again.', ['status' => 429]);
    $config = dotmac_fiber_upstream_config();
    if (!$config) return new WP_Error('fiber_integration_unavailable', 'Online coverage checks are temporarily unavailable. Please use live chat, or continue on WhatsApp.', ['status' => 503]);
    $input = (array) $request->get_json_params();
    try {
        $id = dotmac_fiber_identity($input);
        $fingerprint = hash('sha256', wp_json_encode(dotmac_fiber_canonical($input), JSON_UNESCAPED_SLASHES));
        $result = dotmac_fiber_outbox()->deliver($id, $fingerprint, static function () use ($request, $id, $fingerprint, $config): DotmacFiberEnvelope {
            $payload = dotmac_fiber_build_payload($request);
            if ($payload instanceof WP_Error) throw new DotmacFiberPayloadException($payload);
            $url = rtrim($config['base_url'], '/') . '/api/v1/webhooks/fiber-inquiry/' . rawurlencode($config['binding_id']);
            return new DotmacFiberEnvelope($fingerprint, wp_json_encode($payload, JSON_UNESCAPED_SLASHES), 'fiber-' . $id, $url);
        }, 'dotmac_fiber_send_envelope', time());
        return dotmac_fiber_delivery_response($result);
    } catch (DotmacFiberPayloadException $error) { return $error->error; }
    catch (DotmacFiberDeliveryException $error) {
        $conflict = $error->getMessage() === 'submission_content_conflict';
        return new WP_Error($conflict ? 'fiber_submission_conflict' : 'fiber_delivery_unavailable',
            $conflict ? 'This submission already contains different details. Continue checking the original pending request.'
                : 'We cannot confirm receipt. Keep this pending request and contact Dotmac for help.', ['status' => $conflict ? 409 : 503]);
    }
}

function dotmac_fiber_submission_status(WP_REST_Request $request): WP_REST_Response|WP_Error {
    try {
        $id = dotmac_fiber_identity((array) $request->get_json_params());
        return dotmac_fiber_delivery_response(dotmac_fiber_outbox()->deliver($id, '',
            static function () { throw new DotmacFiberDeliveryException('missing_envelope'); }, 'dotmac_fiber_send_envelope', time()));
    } catch (DotmacFiberDeliveryException $error) {
        return new WP_Error('fiber_confirmation_unavailable', 'Confirmation is temporarily unavailable. Keep your pending request.', ['status' => 503]);
    }
}

if (defined('WP_CLI') && WP_CLI) {
    WP_CLI::add_command('fiber-outbox run', static function (): void {
        WP_CLI::log(wp_json_encode(dotmac_fiber_outbox()->run('dotmac_fiber_send_envelope', time())));
    });
    WP_CLI::add_command('fiber-outbox inventory', static function (): void {
        WP_CLI::log(wp_json_encode(dotmac_fiber_outbox()->inventory()));
    });
    WP_CLI::add_command('fiber-outbox recover', static function (array $args): void {
        if (count($args) !== 1) WP_CLI::error('Supply one inventory ID. Recovery preserves the original bytes and delivery ID.');
        dotmac_fiber_outbox()->recover($args[0], time());
        WP_CLI::success('Original delivery scheduled for one bounded recovery cycle.');
    });
}

add_action('rest_api_init', static function () {
    register_rest_route('dotmac-fiber/v1', '/attribution', array(
        'methods' => WP_REST_Server::CREATABLE,
        'callback' => 'dotmac_fiber_capture_attribution',
        'permission_callback' => '__return_true',
    ));
    register_rest_route('dotmac-fiber/v1', '/submission-status', array(
        'methods' => WP_REST_Server::CREATABLE, 'callback' => 'dotmac_fiber_submission_status',
        'permission_callback' => '__return_true',
    ));
    register_rest_route('dotmac-fiber/v1', '/quote-request', array(
        'methods' => WP_REST_Server::CREATABLE,
        'callback' => 'dotmac_fiber_quote_request',
        'permission_callback' => '__return_true',
    ));
});
