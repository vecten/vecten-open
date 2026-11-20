# Explicitly terminal object failures

A connector SHALL be able to mark an object failure as terminal. The sync task SHALL fail on that attempt and run its configured failure callback. Other extraction failures SHALL keep their configured retries.
