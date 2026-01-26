"""add wireless site survey tables

Revision ID: c7d8a9b0e1f2
Revises: 980c97473791
Create Date: 2026-01-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ENUM


# revision identifiers, used by Alembic.
revision: str = "c7d8a9b0e1f2"
down_revision: Union[str, None] = "980c97473791"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Create enum types
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE surveystatus AS ENUM (
                'draft',
                'in_progress',
                'completed',
                'archived'
            );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE surveypointtype AS ENUM (
                'tower',
                'access_point',
                'cpe',
                'repeater',
                'custom'
            );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
        """
    )

    # Create wireless_site_surveys table
    if "wireless_site_surveys" not in existing_tables:
        op.create_table(
            "wireless_site_surveys",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column(
                "status",
                ENUM(
                    "draft",
                    "in_progress",
                    "completed",
                    "archived",
                    name="surveystatus",
                    create_type=False,
                ),
                default="draft",
            ),
            sa.Column("min_latitude", sa.Float, nullable=True),
            sa.Column("min_longitude", sa.Float, nullable=True),
            sa.Column("max_latitude", sa.Float, nullable=True),
            sa.Column("max_longitude", sa.Float, nullable=True),
            sa.Column("frequency_mhz", sa.Float, nullable=True),
            sa.Column("default_antenna_height_m", sa.Float, default=10.0),
            sa.Column("default_tx_power_dbm", sa.Float, default=20.0),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("metadata", sa.JSON, nullable=True),
            sa.Column("created_by_id", UUID(as_uuid=True), sa.ForeignKey("people.id"), nullable=True),
            sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )

    # Create survey_points table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS survey_points (
            id UUID PRIMARY KEY,
            survey_id UUID NOT NULL REFERENCES wireless_site_surveys(id) ON DELETE CASCADE,
            name VARCHAR(160) NOT NULL,
            point_type surveypointtype DEFAULT 'custom',
            latitude DOUBLE PRECISION NOT NULL,
            longitude DOUBLE PRECISION NOT NULL,
            geom geometry(Point, 4326),
            ground_elevation_m DOUBLE PRECISION,
            elevation_source VARCHAR(50),
            elevation_tile VARCHAR(20),
            antenna_height_m DOUBLE PRECISION DEFAULT 10.0,
            antenna_gain_dbi DOUBLE PRECISION,
            tx_power_dbm DOUBLE PRECISION,
            notes TEXT,
            metadata JSONB,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """
    )

    # Create survey_los_paths table
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS survey_los_paths (
            id UUID PRIMARY KEY,
            survey_id UUID NOT NULL REFERENCES wireless_site_surveys(id) ON DELETE CASCADE,
            from_point_id UUID NOT NULL REFERENCES survey_points(id) ON DELETE CASCADE,
            to_point_id UUID NOT NULL REFERENCES survey_points(id) ON DELETE CASCADE,
            distance_m DOUBLE PRECISION,
            bearing_deg DOUBLE PRECISION,
            has_clear_los BOOLEAN,
            fresnel_clearance_pct DOUBLE PRECISION,
            max_obstruction_m DOUBLE PRECISION,
            obstruction_distance_m DOUBLE PRECISION,
            elevation_profile JSONB,
            free_space_loss_db DOUBLE PRECISION,
            estimated_rssi_dbm DOUBLE PRECISION,
            analysis_timestamp TIMESTAMP WITH TIME ZONE,
            sample_count INTEGER,
            notes TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        );
        """
    )

    # Create indexes
    op.execute("CREATE INDEX IF NOT EXISTS idx_survey_points_survey_id ON survey_points(survey_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_survey_points_geom ON survey_points USING GIST(geom);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_survey_los_paths_survey_id ON survey_los_paths(survey_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_wireless_surveys_status ON wireless_site_surveys(status);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_wireless_surveys_project_id ON wireless_site_surveys(project_id);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS survey_los_paths;")
    op.execute("DROP TABLE IF EXISTS survey_points;")
    op.execute("DROP TABLE IF EXISTS wireless_site_surveys;")
    op.execute("DROP TYPE IF EXISTS surveypointtype;")
    op.execute("DROP TYPE IF EXISTS surveystatus;")
