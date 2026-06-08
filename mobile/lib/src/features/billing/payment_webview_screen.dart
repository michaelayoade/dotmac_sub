import 'package:flutter/material.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../../models/payment_flow.dart';
import '../../models/topup.dart';

/// Provider-agnostic checkout arguments shared by invoice payment and top-up.
class CheckoutArgs {
  CheckoutArgs({
    required this.providerType,
    required this.reference,
    required this.amount,
    required this.currency,
    required this.metadata,
    this.publicKey,
    this.email,
  });

  final String providerType; // 'paystack' | 'flutterwave'
  final String reference;
  final double amount;
  final String currency;
  final Map<String, String> metadata;
  final String? publicKey;
  final String? email;

  /// Pay one invoice — the provider tx carries the invoice id for verification.
  factory CheckoutArgs.invoice(PaymentInitiation i) => CheckoutArgs(
        providerType: i.providerType,
        reference: i.paymentReference,
        amount: i.amount,
        currency: i.currency,
        publicKey: i.providerPublicKey,
        email: i.customerEmail,
        metadata: {'invoice_id': i.invoiceId},
      );

  /// Top up the prepaid account — the tx carries the top-up intent id.
  factory CheckoutArgs.topup(TopupInitiation t) => CheckoutArgs(
        providerType: t.providerType,
        reference: t.paymentReference,
        amount: t.amount,
        currency: t.currency,
        publicKey: t.providerPublicKey,
        email: t.customerEmail,
        metadata: {
          'payment_flow': 'account_topup',
          'topup_intent_id': t.intentId,
        },
      );
}

/// Hosts the payment provider's inline checkout (Paystack or Flutterwave) in a
/// WebView. On a successful charge the provider callback redirects to a
/// `dotmacpay://` sentinel which we intercept; the screen then pops the
/// reference back to the caller (which verifies it). Pops `null` on cancel.
class PaymentWebViewScreen extends StatefulWidget {
  const PaymentWebViewScreen({super.key, required this.args});

  final CheckoutArgs args;

  @override
  State<PaymentWebViewScreen> createState() => _PaymentWebViewScreenState();
}

class _PaymentWebViewScreenState extends State<PaymentWebViewScreen> {
  late final WebViewController _controller;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setNavigationDelegate(
        NavigationDelegate(
          onPageFinished: (_) {
            if (mounted) setState(() => _loading = false);
          },
          onNavigationRequest: _handleNavigation,
        ),
      )
      ..loadHtmlString(
        _checkoutHtml(widget.args),
        baseUrl: 'https://checkout.dotmac.local/',
      );
  }

  NavigationDecision _handleNavigation(NavigationRequest request) {
    final url = request.url;
    if (url.startsWith('dotmacpay://success')) {
      final reference =
          Uri.parse(url).queryParameters['reference'] ?? widget.args.reference;
      Navigator.of(context).pop(reference);
      return NavigationDecision.prevent;
    }
    if (url.startsWith('dotmacpay://cancel')) {
      Navigator.of(context).pop();
      return NavigationDecision.prevent;
    }
    return NavigationDecision.navigate;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Complete payment')),
      body: Stack(
        children: [
          WebViewWidget(controller: _controller),
          if (_loading) const Center(child: CircularProgressIndicator()),
        ],
      ),
    );
  }
}

String _jsObject(Map<String, String> m) =>
    '{${m.entries.map((e) => '"${e.key}":"${e.value}"').join(',')}}';

String _checkoutHtml(CheckoutArgs a) {
  final email = a.email ?? '';
  final key = a.publicKey ?? '';
  final ref = a.reference;
  final currency = a.currency;
  final meta = _jsObject(a.metadata);

  if (a.providerType == 'flutterwave') {
    final amount = a.amount.toStringAsFixed(2); // major units
    return '''
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"/>
<script src="https://checkout.flutterwave.com/v3.js"></script></head>
<body><script>
  FlutterwaveCheckout({
    public_key: "$key",
    tx_ref: "$ref",
    amount: $amount,
    currency: "$currency",
    customer: { email: "$email" },
    meta: $meta,
    callback: function(data){
      window.location.href = "dotmacpay://success?reference=" + (data.tx_ref || "$ref");
    },
    onclose: function(){ window.location.href = "dotmacpay://cancel"; }
  });
</script></body></html>''';
  }

  // Default: Paystack. Amount is in the minor unit (kobo).
  final amountMinor = (a.amount * 100).round();
  return '''
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"/>
<script src="https://js.paystack.co/v1/inline.js"></script></head>
<body><script>
  var handler = PaystackPop.setup({
    key: "$key",
    email: "$email",
    amount: $amountMinor,
    ref: "$ref",
    currency: "$currency",
    metadata: $meta,
    callback: function(response){
      window.location.href = "dotmacpay://success?reference=" + response.reference;
    },
    onClose: function(){ window.location.href = "dotmacpay://cancel"; }
  });
  handler.openIframe();
</script></body></html>''';
}
