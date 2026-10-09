<?php
/**
 * Plugin Name: Dotmac Fiber Acquisition
 * Description: First-party campaign attribution and signed Selfcare quote relay.
 * Version: 1.1.0
 */

if (!defined('ABSPATH')) {
    exit;
}

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

function dotmac_fiber_submission_key($input, $secret) {
    $candidate = dotmac_fiber_clean($input['submission_id'] ?? '', 120);
    if ($candidate === '') {
        $candidate = wp_generate_uuid4();
    }
    return hash_hmac('sha256', $candidate, $secret);
}

function dotmac_fiber_quote_request(WP_REST_Request $request) {
    if (!dotmac_fiber_rate_limit()) {
        return new WP_Error('fiber_rate_limited', 'Please wait a few minutes before trying again.', array('status' => 429));
    }
    $config = dotmac_fiber_upstream_config();
    if (!$config) {
        return new WP_Error('fiber_integration_unavailable', 'Online coverage checks are temporarily unavailable. Please use live chat, or continue on WhatsApp.', array('status' => 503));
    }
    $input = (array) $request->get_json_params();
    foreach (array('full_name', 'phone', 'address') as $field) {
        if (empty($input[$field])) {
            return new WP_Error('fiber_missing_field', 'Please provide your installation address, name, and phone number.', array('status' => 422));
        }
    }
    if (!empty($input['email']) && !is_email($input['email'])) {
        return new WP_Error('fiber_invalid_email', 'Please enter a valid email address.', array('status' => 422));
    }
    $submission_key = dotmac_fiber_submission_key($input, $config['secret']);
    $cached = get_transient('dotmac_fiber_submission_' . $submission_key);
    if (is_array($cached)) {
        return new WP_REST_Response($cached, 200);
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
    $body = wp_json_encode($payload, JSON_UNESCAPED_SLASHES);
    // Keep retries tied to the same delivery so the receiving system can deduplicate them.
    $delivery_id = 'fiber-' . $submission_key;
    $signature = $config['signature_prefix'] . hash_hmac('sha256', $body, $config['secret']);
    $url = rtrim($config['base_url'], '/') . '/api/v1/webhooks/fiber-inquiry/' . rawurlencode($config['binding_id']);
    $response = wp_remote_post($url, array(
        'timeout' => 20,
        'headers' => array(
            'Content-Type' => 'application/json',
            $config['signature_header'] => $signature,
            $config['delivery_header'] => $delivery_id,
        ),
        'body' => $body,
    ));
    if (is_wp_error($response)) {
        return new WP_Error('fiber_upstream_unavailable', 'We could not confirm whether your enquiry was received. Please contact live chat or WhatsApp before sending it again.', array('status' => 503));
    }
    $status = (int) wp_remote_retrieve_response_code($response);
    $decoded = json_decode(wp_remote_retrieve_body($response), true);
    if ($status < 200 || $status >= 300 || !is_array($decoded)) {
        return new WP_Error('fiber_upstream_rejected', 'We could not complete the automated check. Please use live chat, or continue on WhatsApp.', array('status' => 503));
    }
    $coverage = $decoded['coverage'] ?? null;
    $valid_states = array('covered', 'survey_required', 'out_of_area', 'manual_review', 'technical_error');
    if (!is_string($decoded['reference'] ?? null) || trim($decoded['reference']) === '' ||
        !is_array($coverage) || !in_array($coverage['status'] ?? null, $valid_states, true) ||
        !is_string($coverage['summary'] ?? null) || trim($coverage['summary']) === '') {
        return new WP_Error('fiber_upstream_contract', 'We could not confirm the enquiry result. Please contact live chat or WhatsApp before sending it again.', array('status' => 503));
    }
    $decoded['enquiry_status'] = 'received';
    set_transient('dotmac_fiber_submission_' . $submission_key, $decoded, DAY_IN_SECONDS);
    return new WP_REST_Response($decoded, 200);
}

add_action('rest_api_init', static function () {
    register_rest_route('dotmac-fiber/v1', '/attribution', array(
        'methods' => WP_REST_Server::CREATABLE,
        'callback' => 'dotmac_fiber_capture_attribution',
        'permission_callback' => '__return_true',
    ));
    register_rest_route('dotmac-fiber/v1', '/quote-request', array(
        'methods' => WP_REST_Server::CREATABLE,
        'callback' => 'dotmac_fiber_quote_request',
        'permission_callback' => '__return_true',
    ));
});
