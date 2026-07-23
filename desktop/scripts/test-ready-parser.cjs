"use strict";
const assert = require("assert/strict");
const { validReady } = require("../src/ready.cjs");
assert.deepEqual(validReady('READY {"port":8123,"version":"0.1.0"}'), { port: 8123, version: "0.1.0" });
for (const value of ["", "READY nope", 'READY {"port":0}', 'READY {"port":"8123"}', 'noise {"port":8123}']) assert.equal(validReady(value), null);
console.log("ready parser OK");
