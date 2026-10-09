<?php
// Isolated contract harness: no WordPress bootstrap, database, or HTTP calls.
define('ABSPATH', __DIR__);
define('DAY_IN_SECONDS', 86400);
define('MINUTE_IN_SECONDS', 60);
define('DOTMAC_FIBER_SELFCARE_URL', 'https://selfcare.example.test');
define('DOTMAC_FIBER_BINDING_ID', '00000000-0000-4000-8000-000000000001');
define('DOTMAC_FIBER_SIGNING_SECRET', 'isolated-test-fixture');
class WP_REST_Request {
    private $body = '{}';
    function __construct($method = 'POST') {}
    function set_header($key, $value) {}
    function set_body($value) { $this->body = $value; }
    function get_json_params() { return json_decode($this->body, true); }
}
class WP_REST_Response {
    public $data;
    public $status;
    function __construct($data, $status) { $this->data = $data; $this->status = $status; }
}
class WP_Error {
    public $code;
    function __construct($code, $message, $data = []) { $this->code = $code; }
}
function add_action(...$args) {}
function sanitize_text_field($v) { return trim(strip_tags((string)$v)); }
function sanitize_textarea_field($v) { return trim(strip_tags((string)$v)); }
function wp_parse_url($v, $component) { return parse_url($v, $component); }
function wp_salt($v) { return 'isolated-cookie-fixture'; }
function is_ssl() { return true; }
function wp_json_encode($v, $flags = 0) { return json_encode($v, $flags); }
function wp_generate_uuid4() { return '11111111-1111-4111-8111-111111111111'; }
function is_email($v) { return filter_var($v, FILTER_VALIDATE_EMAIL); }
function sanitize_email($v) { return $v; }
function get_transient($key) {
    return str_starts_with($key, 'dotmac_fiber_rate_') ? 0 : ($GLOBALS['cache'][$key] ?? false);
}
function set_transient($key, $value, $ttl) { $GLOBALS['cache'][$key] = $value; }
function is_wp_error($v) { return $v instanceof WP_Error; }
function wp_remote_retrieve_response_code($v) { return $v['status']; }
function wp_remote_retrieve_body($v) { return $v['body']; }
function wp_remote_post($url, $args) {
    $GLOBALS['calls']++;
    $GLOBALS['wire'] = json_decode($args['body'], true);
    $expected = 'sha256=' . hash_hmac('sha256', $args['body'], DOTMAC_FIBER_SIGNING_SECRET);
    check(hash_equals($expected, $args['headers']['X-Dotmac-Fiber-Signature']), 'body/signature equality');
    return $GLOBALS['upstream'];
}
function check($condition, $name) { if (!$condition) { throw new RuntimeException('FAILED: ' . $name); } }
function request($overrides = []) {
    $input = array_merge([
        'submission_id' => 'isolated-contract-case', 'full_name' => 'Isolated Fixture',
        'phone' => '+2348000000000', 'address' => 'Isolated fixture address',
        'service' => 'home', 'attribution' => ['landing_path' => '/coverage/?utm_source=test', 'utm_source' => 'fixture'],
    ], $overrides);
    $request = new WP_REST_Request();
    $request->set_body(json_encode($input));
    return $request;
}
require __DIR__ . '/dotmac-fiber-acquisition.php';
$GLOBALS['calls'] = 0;
$GLOBALS['cache'] = [];
$GLOBALS['upstream'] = ['status' => 200, 'body' => json_encode([
    'reference' => 'FBR-ISOLATED', 'coverage' => ['status' => 'manual_review', 'summary' => 'Address needs review.'],
])];
$response = dotmac_fiber_quote_request(request());
check($response instanceof WP_REST_Response && $response->status === 200, 'minimum fields accepted');
check($GLOBALS['wire']['form_version'] === 'fiber-coverage-v1', 'supported form version');
check(!isset($GLOBALS['wire']['service']), 'no unsupported service field');
check(str_contains($GLOBALS['wire']['message'], 'home'), 'service preference retained');
check(isset($GLOBALS['wire']['attribution']['journey_id']), 'attribution captured without cookie');
check($GLOBALS['wire']['attribution']['landing_path'] === '/coverage/', 'attribution path normalized');
check(!isset($GLOBALS['wire']['email'], $GLOBALS['wire']['selected_plan']), 'optional fields omitted');
$captured_wire = $GLOBALS['wire'];
$calls = $GLOBALS['calls'];
$replay = dotmac_fiber_quote_request(request());
check($GLOBALS['calls'] === $calls && $replay->data['reference'] === 'FBR-ISOLATED', 'receipt replay without second relay');
$bad = dotmac_fiber_quote_request(request(['submission_id' => 'bad-coordinates', 'latitude' => '91', 'longitude' => '7']));
check($bad instanceof WP_Error && $bad->code === 'fiber_invalid_location' && $GLOBALS['calls'] === $calls, 'invalid coordinates rejected before HTTP');
$GLOBALS['upstream']['body'] = json_encode(['reference' => 'FBR-TECHNICAL', 'coverage' => ['status' => 'technical_error', 'summary' => 'Enquiry received; automated check failed.']]);
$technical = dotmac_fiber_quote_request(request(['submission_id' => 'technical-error']));
check($technical instanceof WP_REST_Response && $technical->data['enquiry_status'] === 'received', 'technical failure retains accepted enquiry');
$GLOBALS['upstream']['body'] = json_encode(['success' => true]);
$malformed = dotmac_fiber_quote_request(request(['submission_id' => 'malformed-response']));
check($malformed instanceof WP_Error && $malformed->code === 'fiber_upstream_contract', 'malformed receipt never reported received');
echo json_encode(['checks' => 'passed', 'synthetic_wire_payload' => $captured_wire]), "\n";
