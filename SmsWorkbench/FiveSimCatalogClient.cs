using System.Globalization;
using System.Net.Http.Headers;

namespace SmsWorkbench
{
    internal static class FiveSimCatalogClient
    {
        internal const string DefaultEndpoint = "https://5sim.net/v1";
        internal const string OpenAiProduct = "openai";

        internal static async Task<IReadOnlyList<FiveSimCountryChoice>> LoadOpenAiCatalogAsync(
            HttpClient httpClient,
            string apiKey,
            string endpoint,
            string product = OpenAiProduct)
        {
            string prices = await GetJsonAsync(httpClient, endpoint, apiKey, "/guest/prices?product=" + Uri.EscapeDataString(product));
            return ParsePriceCatalog(prices, product);
        }

        internal static async Task<string> LoadBalanceAsync(HttpClient httpClient, string apiKey, string endpoint)
        {
            string body = await GetJsonAsync(httpClient, endpoint, apiKey, "/user/profile");
            using JsonDocument document = JsonDocument.Parse(body);
            if (document.RootElement.ValueKind != JsonValueKind.Object
                || !document.RootElement.TryGetProperty("balance", out JsonElement balance))
            {
                throw new InvalidDataException(body.Length > 160 ? body[..160] : body);
            }
            return balance.ValueKind == JsonValueKind.Number ? balance.ToString() : balance.GetString() ?? "";
        }

        private static IReadOnlyList<FiveSimCountryChoice> ParsePriceCatalog(string json, string product)
        {
            var countries = new List<FiveSimCountryChoice>();
            using JsonDocument document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidDataException("5sim 价格接口未返回国家列表");
            }

            // /guest/prices?product=X returns {product: {country: {operator: {...}}}}
            // (product-first), while the unfiltered /guest/prices is country-first.
            // Detect the product-first shape so the product-filtered catalog works.
            JsonElement catalog = document.RootElement;
            if (catalog.TryGetProperty(product, out JsonElement productNode)
                && productNode.ValueKind == JsonValueKind.Object)
            {
                catalog = productNode;
            }

            foreach (JsonProperty countryProperty in catalog.EnumerateObject())
            {
                var operators = countryProperty.Value.EnumerateObject()
                    .Select(ParseOperator)
                    .Where(item => item != null && item.Count > 0)
                    .OrderBy(item => item.NumericPrice)
                    .ToList();
                if (operators.Count == 0) continue;

                countries.Add(new FiveSimCountryChoice(countryProperty.Name, operators));
            }

            return countries
                .OrderBy(item => item.Id, StringComparer.OrdinalIgnoreCase)
                .ToList();
        }

        private static FiveSimOperatorChoice ParseOperator(JsonProperty property)
        {
            if (property.Value.ValueKind != JsonValueKind.Object)
            {
                return null;
            }
            decimal price = property.Value.TryGetProperty("cost", out JsonElement cost)
                && decimal.TryParse(cost.ToString(), NumberStyles.Number, CultureInfo.InvariantCulture, out decimal parsed)
                    ? parsed
                    : 0m;
            int count = property.Value.TryGetProperty("count", out JsonElement countElement)
                ? JsonInteger(countElement)
                : 0;
            string rate = property.Value.TryGetProperty("rate", out JsonElement rateElement)
                ? rateElement.ToString()
                : "";
            if (count <= 0)
            {
                return null;
            }
            return new FiveSimOperatorChoice(property.Name, price, count, rate);
        }

        private static async Task<string> GetJsonAsync(
            HttpClient httpClient,
            string endpoint,
            string apiKey,
            string path)
        {
            string baseUrl = (endpoint ?? DefaultEndpoint).Trim().TrimEnd('/');
            if (string.IsNullOrWhiteSpace(baseUrl)) baseUrl = DefaultEndpoint;
            string url = baseUrl + path;

            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
            request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));

            using HttpResponseMessage response = await httpClient.SendAsync(request);
            string body = (await response.Content.ReadAsStringAsync()).Trim();
            if ((int)response.StatusCode >= 400)
            {
                throw new InvalidDataException(body.Length > 160 ? body[..160] : body);
            }
            return body;
        }

        private static int JsonInteger(JsonElement element)
        {
            if (element.ValueKind == JsonValueKind.Number && element.TryGetInt32(out int number)) return number;
            return int.TryParse(element.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out number) ? number : 0;
        }
    }

    internal sealed class FiveSimCountryChoice
    {
        internal FiveSimCountryChoice(string id, IReadOnlyList<FiveSimOperatorChoice> operators)
        {
            Id = id;
            Operators = operators;
        }

        public string Id { get; }
        public IReadOnlyList<FiveSimOperatorChoice> Operators { get; }
        public string DisplayName => Id;
    }

    internal sealed class FiveSimOperatorChoice
    {
        internal FiveSimOperatorChoice(string id, decimal price, int count, string rate)
        {
            Id = id;
            Price = price;
            Count = count;
            Rate = rate;
            NumericPrice = price;
        }

        public string Id { get; }
        public decimal Price { get; }
        public int Count { get; }
        public string Rate { get; }
        public decimal NumericPrice { get; }
        public string DisplayName
        {
            get
            {
                string rateSuffix = string.IsNullOrWhiteSpace(Rate)
                    ? ""
                    : $" · 到达率 {Rate}%";
                return $"${Price.ToString("0.########", CultureInfo.InvariantCulture)} / 个 · 库存 {Count}{rateSuffix}";
            }
        }
    }
}