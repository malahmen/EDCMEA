const cron = require("node-cron");
const request = require("request");
const cheerio = require("cheerio");
const gSendConfig = require("./gSendOptions.json");
const gsend = require("gmail-send")(gSendConfig);

let lastRequestTime = Date.now();

function logSendMailResults(error, result, fullResult) {
    if (error) {
        console.log(error);
    }
}

function soundTheTresholdAlarm(message) {
    // any email package to set, is set here
    gsend({text: message}, logSendMailResults);
}

// scraps the price
function fetchPriceValue(body) {
    let $ = cheerio.load(body);
    let domPriceElement = $("div.panel-body > div > div:nth-child(3) > div.overview-value.number").html();
    domPriceElement = domPriceElement.trim().split(/\s+/);
    domPriceElement = domPriceElement[0];
    domPriceElement = domPriceElement.trim().split(/\,+/);
    domPriceElement = domPriceElement[0] + domPriceElement[1];
    return parseInt(domPriceElement);
}

// scraps the system
function fetchLocationSystem(body) {
    let $ = cheerio.load(body);
    return $(".overview-system > strong:nth-child(2) > a:nth-child(1)").html();
}

// scraps the station
function fetchLocationStation(body) {
    let $ = cheerio.load(body);
    return $(".overview-station > strong:nth-child(2) > a:nth-child(1)").html();
}

function analyseResult(error, response, body) {
    let priceTreshold = 1600;
    if (!error && response.statusCode == 200) {
        let price = fetchPriceValue(body);
        console.log("current price detected: " + price + " at " + lastRequestTime);
        if (price >= priceTreshold) {
            let system = fetchLocationSystem(body);
            let station = fetchLocationStation(body);
            let message = price + " " + station + " " + system;
            soundTheTresholdAlarm(message);
        }
    }
}

function fetchData() {
    lastRequestTime = Date.now();
    // the url for low temperature diamonds on elite dangerous
    let options = {
        url: 'https://eddb.io/commodity/276'
    };

    request(options, analyseResult);
}

function generateNumberBetween(min, max) {
    return Math.floor(Math.random() * (max - min) + min);
}

function getTimeAnalysis() {
    let currentTime = Date.now();
    return Math.floor(currentTime/1000) - Math.floor(lastRequestTime/1000);
}

function checkMarketPrices() {
    let requestTimeTreshold = generateNumberBetween(60,601); // 1 minute, 10 minutes, in seconds.
    let timeResult = getTimeAnalysis();
    if (timeResult > requestTimeTreshold) {
        fetchData();
    }
}

cron.schedule("* * * * *", checkMarketPrices);
