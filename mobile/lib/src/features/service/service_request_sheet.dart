import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../models/service_request_option.dart';
import '../../models/subscription.dart';
import '../../providers/data_providers.dart';

class ServiceRequestSheet extends ConsumerStatefulWidget {
  const ServiceRequestSheet({
    super.key,
    this.sourceSubscriptionId,
  });

  /// The service already selected on the Service tab. A null value supports
  /// direct navigation to the quote list by using the shared current service.
  final String? sourceSubscriptionId;

  @override
  ConsumerState<ServiceRequestSheet> createState() =>
      _ServiceRequestSheetState();
}

class _ServiceRequestSheetState extends ConsumerState<ServiceRequestSheet> {
  ServiceRequestKind? _kind;
  ServiceRequestOption? _option;
  String? _destinationOfferId;

  @override
  Widget build(BuildContext context) {
    final source = widget.sourceSubscriptionId == null
        ? ref.watch(displayedServiceProvider)
        : ref.watch(subscriptionsProvider).whenData((page) {
            for (final service in page.items) {
              if (service.id == widget.sourceSubscriptionId) return service;
            }
            return null;
          });
    final sourceService = source.asData?.value;
    final isRelocation = _kind == ServiceRequestKind.relocation;
    final available = ServiceRequestOption.values
        .where((option) => option.kind == _kind)
        .where((option) =>
            !isRelocation ||
            (sourceService != null &&
                _matchesCurrentService(option, sourceService)))
        .toList();
    final selectedOption = available.contains(_option) ? _option : null;
    final needsDestinationPlan = selectedOption?.changesTechnology ?? false;
    final planOptions = needsDestinationPlan && sourceService?.isActive == true
        ? ref.watch(relocationPlansProvider(
            (sourceService!.id, selectedOption!.destinationAccessType)))
        : null;
    final canContinue = selectedOption != null &&
        (!isRelocation ||
            (sourceService?.isActive == true &&
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
                  _destinationOfferId = null;
                }),
              ),
              if (isRelocation) ...[
                const SizedBox(height: 16),
                if (source.isLoading)
                  const LinearProgressIndicator()
                else if (source.hasError)
                  const Text('Could not load your services. Please try again.')
                else if (sourceService == null)
                  const Text(
                    'You need an existing service to request a relocation.',
                  )
                else ...[
                  Text(
                    'Relocating ${sourceService.displayName}',
                    style: Theme.of(context).textTheme.titleSmall,
                  ),
                  if (!sourceService.isActive) ...[
                    const SizedBox(height: 8),
                    const Text(
                      'This service is not active and cannot be relocated. '
                      'Reactivate it or contact support.',
                    ),
                  ],
                ],
              ],
              if (_kind != null &&
                  (!isRelocation || sourceService?.isActive == true)) ...[
                const SizedBox(height: 16),
                if (available.isEmpty)
                  const Text(
                    'No relocation types are available for this service.',
                  )
                else ...[
                  DropdownButtonFormField<ServiceRequestOption>(
                    key: ValueKey(
                      'service-option-${_kind!.name}-${sourceService?.id}',
                    ),
                    initialValue: selectedOption,
                    isExpanded: true,
                    decoration: InputDecoration(
                      labelText: isRelocation
                          ? 'Relocation type'
                          : 'Installation type',
                      border: const OutlineInputBorder(),
                    ),
                    items: [
                      for (final option in available)
                        DropdownMenuItem(
                          value: option,
                          child: Text(
                            option.label,
                            overflow: TextOverflow.ellipsis,
                          ),
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
                ],
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
                            option: selectedOption,
                            subscriptionId:
                                isRelocation ? sourceService?.id : null,
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
    return service.offerAccessType == option.sourceAccessType;
  }
}
