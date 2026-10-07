# Downstream repositories for the added providers

These are consumer repositories, not the providers' SDK implementation repos. They
are useful scan targets because their source or manifests reference the provider
package directly. Verify the default branch and package path with `depfix scan`
before forking; monorepos may require a path-specific scan later.

## Google Cloud

| Repository | Evidence / likely package |
| --- | --- |
| [dailydotdev/daily-api](https://github.com/dailydotdev/daily-api) | Node service; manifest includes `@google-cloud/storage`, `@google-cloud/pubsub`, and BigQuery. |
| [ill-inc/biomes-game](https://github.com/ill-inc/biomes-game) | Node monorepo; manifest includes Google Cloud Pub/Sub, Secret Manager, and Monitoring clients. |
| [outsideris/citizen](https://github.com/outsideris/citizen) | Node application; manifest and `storages/gcs.js` use `@google-cloud/storage` and `new Storage()`. |

## Elasticsearch

These are downstream integrations (useful because they exercise real client calls,
even though they are not large end-user applications).

| Repository | Evidence / likely package |
| --- | --- |
| [fastify/fastify-elasticsearch](https://github.com/fastify/fastify-elasticsearch) | Fastify plugin; manifest supports `@elastic/elasticsearch` v8/v9 and creates a client. |
| [nestjs/elasticsearch](https://github.com/nestjs/elasticsearch) | Nest integration; installation and examples use `@elastic/elasticsearch`. |
| [pinojs/pino-elasticsearch](https://github.com/pinojs/pino-elasticsearch) | Logging transport; manifest depends on `@elastic/elasticsearch` and sends records to Elasticsearch. |

## PostgreSQL

| Repository | Evidence / likely package |
| --- | --- |
| [medusajs/medusa](https://github.com/medusajs/medusa) | Commerce application; application packages and examples use PostgreSQL/`pg`. |
| [strapi/strapi](https://github.com/strapi/strapi) | CMS application; its database layer supports PostgreSQL through the `pg` driver. |
| [directus/directus](https://github.com/directus/directus) | Data platform; PostgreSQL is a supported database client (`DB_CLIENT=pg`). |

## Kafka

| Repository | Evidence / likely package |
| --- | --- |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | Workflow application; `packages/nodes-base` includes KafkaJS and Kafka integrations. |
| [amplication/amplication](https://github.com/amplication/amplication) | Developer platform; manifest includes `kafkajs` and Kafka subscription support. |
| [lydtechconsulting/kafkajs-consume-produce](https://github.com/lydtechconsulting/kafkajs-consume-produce) | Standalone TypeScript application explicitly demonstrating KafkaJS produce/consume and integration tests. |

## RabbitMQ

| Repository | Evidence / likely package |
| --- | --- |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | Workflow application; `packages/nodes-base` includes `amqplib`. |
| [mguay22/nestjs-rabbitmq-microservices](https://github.com/mguay22/nestjs-rabbitmq-microservices) | Nest application; manifest includes `amqplib` and its RMQ service has executable tests. |
| [moscajs/ascoltatori](https://github.com/moscajs/ascoltatori) | Pub/sub application library; its AMQP transport uses `amqplib/callback_api`. |

## Scan commands

After the GitHub App is installed on a fork, run one command per provider/repository:

```bash
depfix scan --repo OWNER/REPO --provider PROVIDER_ID
```

Examples:

```bash
depfix scan --repo dailydotdev/daily-api --provider google-cloud
depfix scan --repo fastify/fastify-elasticsearch --provider elasticsearch
depfix scan --repo medusajs/medusa --provider postgresql
depfix scan --repo lydtechconsulting/kafkajs-consume-produce --provider kafka
depfix scan --repo golevelup/nestjs --provider rabbitmq
```

`scan` is the authoritative check: a repository is a usable target only if it has a
recognized manifest/package and actionable call sites. Some listed projects are
monorepos or integration libraries, so a successful manifest match does not imply
that every file is relevant.
