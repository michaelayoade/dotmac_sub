import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/models/project.dart';

void main() {
  group('ProjectsSummary.fromJson', () {
    test('parses projects with stage timeline and progress', () {
      final summary = ProjectsSummary.fromJson({
        'total': 1,
        'active': 1,
        'projects': [
          {
            'id': 'p1',
            'name': 'Fiber install',
            'status': 'active',
            'project_type': 'fiber_optics_installation',
            'progress_pct': 50,
            'current_stage': 'Drop Cable Installation',
            'customer_address': '12 Test St',
            'stages': [
              {
                'key': 'project_plan',
                'title': 'Project Plan',
                'status': 'done',
              },
              {
                'key': 'drop_cable_installation',
                'title': 'Drop Cable Installation',
                'status': 'in_progress',
              },
            ],
            'created_at': '2026-06-20T09:00:00+00:00',
          },
        ],
      });

      expect(summary.total, 1);
      expect(summary.active, 1);
      final p = summary.projects.single;
      expect(p.name, 'Fiber install');
      expect(p.progressPct, 50);
      expect(p.currentStage, 'Drop Cable Installation');
      expect(p.stages.length, 2);
      expect(p.stages.first.status, 'done');
      expect(p.createdAt, isNotNull);
    });

    test('tolerates an empty payload', () {
      final summary = ProjectsSummary.fromJson({});
      expect(summary.projects, isEmpty);
      expect(summary.total, 0);
      expect(summary.active, 0);
    });
  });
}
