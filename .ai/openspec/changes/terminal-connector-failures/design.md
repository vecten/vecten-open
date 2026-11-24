# Design

Carry the connector's explicit exception through dlt's exception chain as a flag on the object outcome. Only the Airflow task translates that flag into Airflow's immediate failure exception. The unit of loading and the lifetime of dlt storage stay the same.
