enum ServiceRequestKind { installation, relocation }

enum ServiceRequestOption {
  fiberInstallation(
    'fiber_installation',
    ServiceRequestKind.installation,
    'Fiber Installation',
    'Get a new fiber connection at this address.',
  ),
  airfiberInstallation(
    'airfiber_installation',
    ServiceRequestKind.installation,
    'Airfiber Installation',
    'Get a new Airfiber connection at this address, subject to a site check.',
  ),
  fiberToFiberRelocation(
    'fiber_to_fiber_relocation',
    ServiceRequestKind.relocation,
    'Fiber to Fiber Relocation',
    'Move your existing fiber service to a new address and keep fiber.',
  ),
  airfiberToFiberRelocation(
    'airfiber_to_fiber_relocation',
    ServiceRequestKind.relocation,
    'Airfiber to Fiber Relocation',
    'Move your service to a new address and change from Airfiber to fiber.',
  ),
  fiberToAirfiberRelocation(
    'fiber_to_airfiber_relocation',
    ServiceRequestKind.relocation,
    'Fiber to Airfiber Relocation',
    'Move your service to a new address and change from fiber to Airfiber.',
  ),
  airfiberToAirfiberNoCable(
    'airfiber_to_airfiber_relocation_no_cable_replacement',
    ServiceRequestKind.relocation,
    'Airfiber to Airfiber Relocation (No cable replacement)',
    'Move your Airfiber service using the existing cable where suitable.',
  ),
  airfiberToAirfiberWithCable(
    'airfiber_to_airfiber_relocation_with_cable_replacement',
    ServiceRequestKind.relocation,
    'Airfiber to Airfiber Relocation (With cable replacement)',
    'Move your Airfiber service and install replacement cable.',
  );

  const ServiceRequestOption(
      this.apiValue, this.kind, this.label, this.description);

  final String apiValue;
  final ServiceRequestKind kind;
  final String label;
  final String description;

  String get destinationAccessType => switch (this) {
        fiberInstallation || fiberToFiberRelocation ||
        airfiberToFiberRelocation => 'fiber',
        _ => 'fixed_wireless',
      };

  String? get sourceAccessType => switch (this) {
        fiberToFiberRelocation || fiberToAirfiberRelocation => 'fiber',
        airfiberToFiberRelocation || airfiberToAirfiberNoCable ||
        airfiberToAirfiberWithCable => 'fixed_wireless',
        _ => null,
      };

  bool get changesTechnology =>
      kind == ServiceRequestKind.relocation &&
      sourceAccessType != destinationAccessType;

  static ServiceRequestOption? fromApiValue(String? value) {
    for (final option in values) {
      if (option.apiValue == value) return option;
    }
    return null;
  }
}

class ServiceRequestSelection {
  const ServiceRequestSelection({
    required this.option,
    this.subscriptionId,
    this.destinationOfferId,
  });

  final ServiceRequestOption option;
  final String? subscriptionId;
  final String? destinationOfferId;
}
