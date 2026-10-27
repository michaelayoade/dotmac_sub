<?php
/**
 * Plugin Name: Dotmac Fiber WhatsApp Enquiry Handoff
 * Description: Signed, durable enquiry-only transport using the existing Fiber outbox.
 * Version: 1.0.0
 */
declare(strict_types=1);

if (!defined('ABSPATH')) { exit; }

/** Typed browser submission identity; transport validation only. */
final class DotmacWhatsAppHandoffSubmission {
    public function __construct(public readonly string $submissionId) {
        if (!preg_match('/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/iD', $submissionId)) {
            throw new InvalidArgumentException('Invalid submission identity');
        }
    }
}

/**
 * Existing producer output is normalized at this adapter boundary before signing.
 * @param array{form_version:string,full_name:string,phone:string,interest:string,
 *   submitted_at:string,location:array{address:string,area?:string,latitude?:string,longitude?:string},
 *   attribution:array<string,string>,message?:string,email?:string,selected_plan?:array{name:string}} $coverage
 */
function dotmac_whatsapp_handoff_body(array $coverage, string $accountId): string {
    if (!preg_match('/^[0-9]{1,32}$/D', $accountId)) {
        throw new InvalidArgumentException('WhatsApp account is not configured');
    }
    // Preserve the producer's explicit field allowlist; do not forward request mappings.
    $wire = [
        'form_version' => 'fiber-whatsapp-v1',
        'full_name' => $coverage['full_name'],
        'phone' => $coverage['phone'],
        'interest' => 'new_connection',
        'submitted_at' => $coverage['submitted_at'],
        'location' => $coverage['location'],
        'attribution' => $coverage['attribution'],
        'whatsapp_account_id' => $accountId,
    ];
    foreach (['message', 'email', 'selected_plan'] as $optional) {
        if (isset($coverage[$optional])) { $wire[$optional] = $coverage[$optional]; }
    }
    return json_encode($wire, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
}

function dotmac_whatsapp_handoff_receive(WP_REST_Request $request): WP_REST_Response|WP_Error {
    if (!function_exists('dotmac_fiber_outbox') || !function_exists('dotmac_fiber_build_payload')) {
        return new WP_Error('fiber_handoff_unavailable', 'Enquiry saving is temporarily unavailable.', ['status' => 503]);
    }
    if (!dotmac_fiber_rate_limit()) {
        return new WP_Error('fiber_rate_limited', 'Please wait a few minutes before trying again.', ['status' => 429]);
    }
    $config = dotmac_fiber_upstream_config();
    $account = defined('DOTMAC_FIBER_WHATSAPP_ACCOUNT_ID') ? (string) DOTMAC_FIBER_WHATSAPP_ACCOUNT_ID : '';
    if (!$config || !preg_match('/^[0-9]{1,32}$/D', $account)) {
        return new WP_Error('fiber_handoff_unavailable', 'Enquiry saving is temporarily unavailable.', ['status' => 503]);
    }
    try {
        $submission = new DotmacWhatsAppHandoffSubmission((string) $request->get_param('submission_id'));
        $input = $request->get_json_params();
        if (!is_array($input)) { throw new InvalidArgumentException('Invalid submission'); }
        $id = dotmac_fiber_identity(['submission_id' => $submission->submissionId]);
        $fingerprint = hash('sha256', wp_json_encode(dotmac_fiber_canonical($input), JSON_UNESCAPED_SLASHES));
        $result = dotmac_fiber_outbox()->deliver($id, $fingerprint,
            static function () use ($request, $id, $fingerprint, $config, $account): DotmacFiberEnvelope {
                $payload = dotmac_fiber_build_payload($request);
                if ($payload instanceof WP_Error) { throw new DotmacFiberPayloadException($payload); }
                $body = dotmac_whatsapp_handoff_body($payload, $account);
                $url = rtrim($config['base_url'], '/') . '/api/v1/webhooks/fiber-inquiry/' . rawurlencode($config['binding_id']);
                return new DotmacFiberEnvelope($fingerprint, $body, 'fiber-' . $id, $url);
            }, 'dotmac_fiber_send_envelope', time());
        return dotmac_fiber_delivery_response($result);
    } catch (DotmacFiberPayloadException $error) {
        return $error->error;
    } catch (InvalidArgumentException $error) {
        return new WP_Error('fiber_invalid_fields', 'Please reload and check your enquiry details.', ['status' => 422]);
    } catch (Throwable $error) {
        return new WP_Error('fiber_handoff_pending', 'Receipt is unconfirmed. Keep this enquiry and retry the same request.', ['status' => 503]);
    }
}

add_action('rest_api_init', static function (): void {
    register_rest_route('dotmac-fiber/v1', '/whatsapp-enquiry', [
        'methods' => WP_REST_Server::CREATABLE,
        'callback' => 'dotmac_whatsapp_handoff_receive',
        'permission_callback' => '__return_true',
    ]);
});
