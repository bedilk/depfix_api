const stripe = require("../lib/stripeClient");

async function createCustomer(email) {
  const customer = await stripe.customers.create({ email });
  return customer.id;
}

async function chargeCustomer(customerId, amountCents) {
  const charge = await stripe.charges.create({
    customer: customerId,
    amount: amountCents,
    currency: "usd",
  });
  return charge.id;
}

module.exports = { createCustomer, chargeCustomer };
