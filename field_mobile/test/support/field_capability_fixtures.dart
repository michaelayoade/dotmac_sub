import 'package:dotmac_field/features/jobs/jobs_providers.dart';

// Explicit server capability evidence for technician UI fixtures.
const availableFieldCapabilities = FieldExecutionCapabilities(
  attendance: FieldCapabilityAvailability(available: true),
  locationTracking: FieldCapabilityAvailability(available: true),
  fiberEvidence: FieldCapabilityAvailability(available: true),
  chat: FieldCapabilityAvailability(available: true),
  materials: FieldCapabilityAvailability(available: true),
  expenses: FieldCapabilityAvailability(available: true),
  equipment: FieldCapabilityAvailability(available: true),
);
