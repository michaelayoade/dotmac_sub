import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/service_request_option.dart';
import '../../models/subscription.dart';
import '../../providers/data_providers.dart';

class ServiceRequestSheet extends ConsumerStatefulWidget {
  const ServiceRequestSheet({super.key});

  @override
  ConsumerState<ServiceRequestSheet> createState() =>
      _ServiceRequestSheetState();
}

class _ServiceRequestSheetState extends ConsumerState<ServiceRequestSheet> {
  ServiceRequestKind? _kind;
  ServiceRequestOption? _option;
  String? _subscriptionId;
  String? _destinationOfferId;

  @override
  Widget build(BuildContext context) {
    final subscriptions = ref.watch(subscriptionsProvider);
    final current = subscriptions.asData?.value.items
            .where((service) => service.isActive)
            .toList() ??
        const <Subscription>[];
    final available = ServiceRequestOption.values
        .where((option) => option.kind == _kind)
        .toList();
    final isRelocation = _kind == ServiceRequestKind.relocation;
    final needsDestinationPlan = _option?.changesTechnology ?? false;
    final planOptions = needsDestinationPlan && _subscriptionId != null
        ? ref.watch(relocationPlansProvider(
            (_subscriptionId!, _option!.destinationAccessType)))
        : null;
    Subscription? selectedService;
    for (final service in current) {
      if (service.id == _subscriptionId) selectedService = service;
    }
    final canContinue = _option != null &&
        (!isRelocation ||
            (selectedService != null &&
                _matchesCurrentService(_option!, selectedService) &&
                (!needsDestinationPlan || _destinationOfferId != null)));

    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          20,
          16,
          20,
          20 + MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text('Request service',
                  style: Theme.of(context).textTheme.headlineSmall),
              const SizedBox(height: 12),
              const Text('What type of service do you need?'),
              const SizedBox(height: 12),
              SegmentedButton<ServiceRequestKind>(
                segments: const [
                  ButtonSegment(
                    value: ServiceRequestKind.installation,
                    label: Text('Installation'),
                  ),
                  ButtonSegment(
                    value: ServiceRequestKind.relocation,
                    label: Text('Relocation'),
                  ),
                ],
                selected: _kind == null ? {} : {_kind!},
                emptySelectionAllowed: true,
                onSelectionChanged: (selected) => setState(() {
                  _kind = selected.isEmpty ? null : selected.first;
                  _option = null;
                  _subscriptionId = null;
                  _destinationOfferId = null;
                }),
              ),
              if (isRelocation) ...[
                const SizedBox(height: 16),
                if (subscriptions.isLoading)
                  const LinearProgressIndicator()
                else if (subscriptions.hasError)
                  const Text('Could not load your services. Please try again.')
                else if (current.isEmpty)
                  const Text(
                      'You need an existing service to request a relocation.')
                else
                  DropdownButtonFormField<String>(
                    key: const ValueKey('relocation-existing-service'),
                    initialValue: _subscriptionId,
                    isExpanded: true,
                    decoration: const InputDecoration(
                      labelText: 'Service to relocate',
                      border: OutlineInputBorder(),
                    ),
                    items: [
                      for (final service in current)
                        DropdownMenuItem(
                          value: service.id,
                          child: Text(service.displayName,
                              overflow: TextOverflow.ellipsis),
                        ),
                    ],
                    onChanged: (value) => setState(() {
                      _subscriptionId = value;
                      _option = null;
                      _destinationOfferId = null;
                    }),
                  ),
              ],
              if (_kind != null) ...[
                const SizedBox(height: 16),
                DropdownButtonFormField<ServiceRequestOption>(
                  key: ValueKey('service-option-${_kind!.name}'),
                  initialValue: _option,
                  isExpanded: true,
                  decoration: InputDecoration(
                    labelText:
                        isRelocation ? 'Relocation type' : 'Installation type',
                    border: const OutlineInputBorder(),
                  ),
                  items: [
                    for (final option in available)
                      DropdownMenuItem(
                        value: option,
                        enabled: !isRelocation ||
                            selectedService == null ||
                            _matchesCurrentService(option, selectedService),
                        child:
                            Text(option.label, overflow: TextOverflow.ellipsis),
                      ),
                  ],
                  onChanged: (value) => setState(() {
                    _option = value;
                    _destinationOfferId = null;
                  }),
                ),
                const SizedBox(height: 12),
                for (final option in available)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Text(
                      '${option.label}: ${option.description}',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                if (needsDestinationPlan) ...[
                  const SizedBox(height: 8),
                  if (planOptions == null || planOptions.isLoading)
                    const LinearProgressIndicator()
                  else if (planOptions.hasError)
                    const Text(
                        'Could not load destination plans. Please try again.')
                  else if (planOptions.value!.isEmpty)
                    const Text(
                        'No destination plans are available for this move.')
                  else
                    DropdownButtonFormField<String>(
                      key: const ValueKey('relocation-destination-plan'),
                      initialValue: _destinationOfferId,
                      isExpanded: true,
                      decoration: const InputDecoration(
                        labelText: 'Plan at your new address',
                        border: OutlineInputBorder(),
                      ),
                      items: [
                        for (final plan in planOptions.value!)
                          DropdownMenuItem(
                            value: plan.id,
                            child: Text(plan.name,
                                overflow: TextOverflow.ellipsis),
                          ),
                      ],
                      onChanged: (value) =>
                          setState(() => _destinationOfferId = value),
                    ),
                ],
              ],
              const SizedBox(height: 24),
              FilledButton(
                onPressed: canContinue
                    ? () => Navigator.of(context).pop(
                          ServiceRequestSelection(
                            option: _option!,
                            subscriptionId: _subscriptionId,
                            destinationOfferId: _destinationOfferId,
                          ),
                        )
                    : null,
                child: const Text('Continue to location'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  bool _matchesCurrentService(
    ServiceRequestOption option,
    Subscription service,
  ) {
    final access = service.offerAccessType;
    if (access == null) return true; // The server will validate the source.
    return access == option.sourceAccessType;
  }
}
