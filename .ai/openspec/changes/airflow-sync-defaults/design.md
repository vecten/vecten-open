# Design

The platform wheel declares the supported connector range. The image build constrains that driver to the workspace lock while resolving the other packages against the upstream Airflow image. The sync package sets the dlt provider default before loading connector code and preserves an explicit environment setting.
