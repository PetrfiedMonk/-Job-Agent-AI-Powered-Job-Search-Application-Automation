(function () {
  "use strict";

  function scrapeLinkedIn() {
    const title =
      document.querySelector(".job-details-jobs-unified-top-card__job-title, h1.t-24, h1[class*='JobTitle']")
        ?.textContent?.trim();
    const company =
      document.querySelector(".job-details-jobs-unified-top-card__company-name a, .topcard__org-name-link, [class*='CompanyName']")
        ?.textContent?.trim();
    const location =
      document.querySelector(".job-details-jobs-unified-top-card__bullet, .topcard__flavor--bullet")
        ?.textContent?.trim();
    return { title, company, location, url: location.href, platform: "linkedin" };
  }

  function scrapeIndeed() {
    const title =
      document.querySelector("[data-testid='jobsearch-JobInfoHeader-title'], h1.jobsearch-JobInfoHeader-title")
        ?.textContent?.trim();
    const company =
      document.querySelector("[data-testid='inlineHeader-companyName'] span, .jobsearch-InlineCompanyRating-companyHeader")
        ?.textContent?.trim();
    const loc =
      document.querySelector("[data-testid='job-location'], .jobsearch-JobInfoHeader-subtitle div:last-child")
        ?.textContent?.trim();
    return { title, company, location: loc, url: window.location.href, platform: "indeed" };
  }

  function scrapeZipRecruiter() {
    const title =
      document.querySelector("h1.job_title, .job-header h1, [class*='JobTitle']")
        ?.textContent?.trim();
    const company =
      document.querySelector(".job_company_name, .hiring_company_text, [class*='CompanyName']")
        ?.textContent?.trim();
    return { title, company, location: "", url: window.location.href, platform: "ziprecruiter" };
  }

  function scrapeGlassdoor() {
    const title =
      document.querySelector("[data-test='job-title'], .JobDetails_jobTitle__Rw_gn, h1[class*='JobTitle']")
        ?.textContent?.trim();
    const company =
      document.querySelector("[data-test='employer-name'], .EmployerProfile_profileContainer__6nRES, [class*='EmployerName']")
        ?.textContent?.trim();
    return { title, company, location: "", url: window.location.href, platform: "glassdoor" };
  }

  function getJobInfo() {
    const host = window.location.hostname;
    if (host.includes("linkedin.com"))    return scrapeLinkedIn();
    if (host.includes("indeed.com"))      return scrapeIndeed();
    if (host.includes("ziprecruiter.com")) return scrapeZipRecruiter();
    if (host.includes("glassdoor.com"))   return scrapeGlassdoor();
    return null;
  }

  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg.type === "GET_JOB") {
      reply(getJobInfo());
    }
  });
})();
