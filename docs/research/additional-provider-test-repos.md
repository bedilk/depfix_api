# Additional provider test repositories

These repositories were verified on 2026-09-19 through the GitHub API. They
are candidates to fork into the `bedilk` account; they are not automatically
forked or added to the GitHub App by changing `providers.yaml`.

| Provider | Repository 1 | Repository 2 | Repository 3 |
| --- | --- | --- | --- |
| Google Cloud | `googleapis/google-api-nodejs-client` | `googleapis/nodejs-storage` (archived) | `googleapis/nodejs-bigquery` (archived) |
| Elasticsearch | `elastic/elasticsearch-js` | `elastic/elasticsearch-py` | `elastic/kibana` |
| PostgreSQL | `brianc/node-postgres` | `psycopg/psycopg` | `psycopg/psycopg2` |
| Kafka | `tulios/kafkajs` | `apache/kafka` | `confluentinc/confluent-kafka-python` |
| RabbitMQ | `amqp-node/amqplib` | `pika/pika` | `rabbitmq/rabbitmq-tutorials` |

## Suggested fork names

```text
bedilk/depfix-google-api-nodejs-client
bedilk/depfix-nodejs-storage
bedilk/depfix-nodejs-bigquery
bedilk/depfix-elasticsearch-js
bedilk/depfix-elasticsearch-py
bedilk/depfix-kibana
bedilk/depfix-node-postgres
bedilk/depfix-psycopg
bedilk/depfix-psycopg2
bedilk/depfix-kafkajs
bedilk/depfix-kafka
bedilk/depfix-confluent-kafka-python
bedilk/depfix-amqplib
bedilk/depfix-pika
bedilk/depfix-rabbitmq-tutorials
```

Archived repositories are useful for historical SDK versions, but a fork
must still be installed in the Depfix GitHub App before remote scans can
clone it.
