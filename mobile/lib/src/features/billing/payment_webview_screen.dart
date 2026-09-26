import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:webview_flutter/webview_flutter.dart';

import '../../config/env.dart';
import '../../core/payment_app_launcher.dart';
import '../../core/payment_navigation.dart';
import '../../core/payment_return_coordinator.dart';
import '../../models/payment_flow.dart';
import '../../models/reseller.dart';
import '../../models/topup.dart';

/// Provider-agnostic checkout arguments shared by invoice payment and top-up.
class CheckoutArgs {
  CheckoutArgs({
    required this.providerType,
    required this.reference,
    required this.amount,
    required this.currency,
    required this.metadata,
    required this.checkoutUrl,
    this.publicKey,
    this.email,
  });

  final String providerType; // 'paystack' | 'flutterwave'
  final String reference;
  final double amount;
  final String currency;
  final Map<String, String> metadata;
  final String checkoutUrl;
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
        checkoutUrl: secureCheckoutUrl(i.checkoutUrl),
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
        checkoutUrl: secureCheckoutUrl(t.checkoutUrl),
        metadata: {
          'payment_flow': 'account_topup',
          'topup_intent_id': t.intentId
        },
      );

  /// Reseller consolidated payment — metadata comes ready-made from the
  /// intent endpoint (payment_flow: reseller_consolidated).
  factory CheckoutArgs.resellerBilling(ResellerPayIntent i) => CheckoutArgs(
        providerType: i.providerType,
        reference: i.reference,
        amount: i.amount,
        currency: i.currency,
        publicKey: i.publicKey,
        checkoutUrl: secureCheckoutUrl(i.checkoutUrl),
        metadata: i.metadata,
      );

  static String secureCheckoutUrl(String? value) {
    final uri = Uri.tryParse(value ?? '');
    if (uri == null || uri.scheme != 'https' || uri.host.isEmpty) {
      throw StateError(
        'The payment provider did not return a secure checkout.',
      );
    }
    return uri.toString();
  }
}

/// Hosts the payment provider's first-party checkout URL in a WebView. On a
/// successful charge the provider callback redirects to an app sentinel or the
/// API's HTTPS verification URL, which we intercept. Native-wallet links are
/// handed to the operating system while this checkout remains on the back
/// stack. The screen then pops the reference back to the caller (which verifies
/// it). Pops `null` on cancel.
class PaymentWebViewScreen extends ConsumerStatefulWidget {
  const PaymentWebViewScreen({
    super.key,
    required this.args,
    this.paymentAppLauncher = const PlatformPaymentAppLauncher(),
  });

  final CheckoutArgs args;
  final PaymentAppLauncher paymentAppLauncher;

  @override
  ConsumerState<PaymentWebViewScreen> createState() =>
      _PaymentWebViewScreenState();
}

class _PaymentWebViewScreenState extends ConsumerState<PaymentWebViewScreen>
    with WidgetsBindingObserver {
  late final WebViewController _controller;
  late final PaymentReturnCoordinator _paymentReturns;
  late final Object _returnListenerToken;
  bool _loading = true;
  bool _allowPop = false;
  bool _completionPending = false;
  bool _awaitingExternalAppReturn = false;
  String? _loadError;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _paymentReturns = ref.read(paymentReturnCoordinatorProvider);
    _returnListenerToken = _paymentReturns.attach(_handleAppReturn);
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setNavigationDelegate(
        NavigationDelegate(
          onPageFinished: (_) {
            if (mounted) setState(() => _loading = false);
          },
          onWebResourceError: (error) {
            if (error.isForMainFrame == true && mounted) {
              setState(() {
                _loading = false;
                _loadError = 'The secure payment page could not be loaded.';
              });
            }
          },
          onNavigationRequest: _handleNavigation,
        ),
      )
      ..loadRequest(Uri.parse(widget.args.checkoutUrl));
  }

  @override
  void dispose() {
    _paymentReturns.detach(_returnListenerToken);
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed ||
        !_awaitingExternalAppReturn ||
        _completionPending) {
      return;
    }
    _awaitingExternalAppReturn = false;
    _restoreCheckout();
  }

  Future<void> _restoreCheckout() async {
    if (!mounted) return;
    setState(() {
      _loading = true;
      _loadError = null;
    });
    try {
      await _controller.loadRequest(Uri.parse(widget.args.checkoutUrl));
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _loadError = 'The secure payment page could not be loaded.';
      });
    }
  }

  bool _handleAppReturn(Uri uri) {
    if (uri.scheme != Brand.paymentScheme) return false;
    if (uri.host == 'cancel') {
      _finishCheckout();
      return true;
    }
    if (uri.host != 'success') return false;

    final returnedReference =
        uri.queryParameters['reference'] ?? uri.queryParameters['trxref'];
    if (returnedReference != null &&
        returnedReference != widget.args.reference) {
      return false;
    }
    _finishCheckout(returnedReference ?? widget.args.reference);
    return true;
  }

  void _finishCheckout([String? reference]) {
    if (!mounted || _completionPending) return;
    _completionPending = true;
    _awaitingExternalAppReturn = false;
    setState(() => _allowPop = true);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) Navigator.of(context).pop<String>(reference);
    });
  }

  Future<NavigationDecision> _handleNavigation(
    NavigationRequest request,
  ) async {
    final target = resolvePaymentNavigation(
      request.url,
      expectedReference: widget.args.reference,
    );
    switch (target.disposition) {
      case PaymentNavigationDisposition.complete:
        _finishCheckout(target.reference ?? widget.args.reference);
        return NavigationDecision.prevent;
      case PaymentNavigationDisposition.cancel:
        _finishCheckout();
        return NavigationDecision.prevent;
      case PaymentNavigationDisposition.navigateInWebView:
        return NavigationDecision.navigate;
      case PaymentNavigationDisposition.tryExternalApp:
        _awaitingExternalAppReturn = true;
        final result = await widget.paymentAppLauncher.launch(
          target.uri!,
          nonBrowserOnly: true,
        );
        if (!result.launched) _awaitingExternalAppReturn = false;
        return result.launched
            ? NavigationDecision.prevent
            : NavigationDecision.navigate;
      case PaymentNavigationDisposition.launchExternalApp:
        _awaitingExternalAppReturn = true;
        final result = await widget.paymentAppLauncher.launch(
          target.uri!,
          nonBrowserOnly: false,
        );
        if (result.launched) return NavigationDecision.prevent;
        _awaitingExternalAppReturn = false;
        final fallbackUrl = result.fallbackUrl;
        if (fallbackUrl != null) {
          await _controller.loadRequest(fallbackUrl);
        } else if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text(
                'The payment app could not be opened. Make sure it is installed, then try again.',
              ),
            ),
          );
        }
        return NavigationDecision.prevent;
      case PaymentNavigationDisposition.block:
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text('This payment link could not be opened safely.'),
            ),
          );
        }
        return NavigationDecision.prevent;
    }
  }

  Future<bool> _confirmLeave() async {
    final leave = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Leave payment?'),
        content: const Text(
          'Your payment is not finished. Leaving now will cancel it.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Stay'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Leave'),
          ),
        ],
      ),
    );
    return leave ?? false;
  }

  @override
  Widget build(BuildContext context) {
    // Guard the system/back-button: while the checkout is active, confirm
    // before popping (and pop `null` = cancelled, matching the cancel sentinel).
    return PopScope(
      canPop: _allowPop,
      onPopInvokedWithResult: (didPop, _) async {
        if (didPop || _allowPop) return;
        if (await _confirmLeave()) _finishCheckout();
      },
      child: Scaffold(
        appBar: AppBar(title: const Text('Complete payment')),
        body: Stack(
          children: [
            if (_loadError == null)
              WebViewWidget(controller: _controller)
            else
              Center(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Text(_loadError!, textAlign: TextAlign.center),
                ),
              ),
            if (_loading) const Center(child: CircularProgressIndicator()),
          ],
        ),
      ),
    );
  }
}
